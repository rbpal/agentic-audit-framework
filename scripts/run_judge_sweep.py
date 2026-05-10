"""Step 6 task_04 judge sweep driver — iterate ``gold.narratives``,
call ``Judge.evaluate(...)`` per row, write the resulting verdict +
metadata to ``audit_dev.gold.judge_outcomes``, and emit one
cost-telemetry summary row to ``audit_dev.gold.cost_telemetry``.

Mirrors the shape of ``scripts/run_layer2.py``:

- ``run_sweep(...)`` is the testable orchestrator. Takes injected
  primitives (narratives iterable, ``Judge``, ``JudgeOutcomesWriter``,
  gold-verdict lookup) so unit tests can mock the world.
- ``main(...)`` wires the env-var auth + Databricks SQL factory +
  Azure OpenAI judge, loads the ToC gold lookup, and calls
  ``run_sweep(...)``.
- Per-narrative errors are caught and logged; the sweep continues.
- Summary print: ``✓ N narratives evaluated; pass/fail/uncertain =
  X/Y/Z`` plus the cost-telemetry totals.

This is an operator tool, not a CI test. Unit tests cover the
orchestrator wiring; the live cloud sweep runs on demand.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentic_audit.layer2_narrative.sweep import iter_narratable_combinations
from agentic_audit.models.evidence import ATTRIBUTE_DEFINITIONS_PER_CONTROL
from agentic_audit.models.judge import JudgeOutcomeRow

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentic_audit.layer2_narrative.judge import Judge
    from agentic_audit.layer2_narrative.silver_reader import SilverEvidenceReader
    from agentic_audit.models.evidence import ExtractedEvidence
    from agentic_audit.models.narrative import AttributeNarrative


def _load_gold_lookup_from_tocs(toc_dir: Path) -> dict[tuple[str, str, str], str]:
    """Read every ``*.json`` file in ``toc_dir`` and return a flat
    ``(control_id, quarter, attribute_id) -> verdict`` lookup.

    Each ToC JSON file's ``expected_attribute_results`` dict has keys
    of the form ``"<control_id>.<attribute_id>"`` (e.g., ``"DC-9.A"``)
    mapping to ``"pass"`` or ``"fail"``. We unpack those into the flat
    keyed lookup so the per-row code path is a single dict access
    instead of a parse + split.

    Called once at sweep start; the resulting dict is passed to
    ``run_sweep``'s ``gold_lookup`` kwarg.
    """
    lookup: dict[tuple[str, str, str], str] = {}
    for path in sorted(toc_dir.glob("*.json")):
        data = json.loads(path.read_text())
        control = data["control_id"]
        quarter = data["quarter"]
        for key, verdict in data["expected_attribute_results"].items():
            attribute = key.split(".", 1)[-1]
            lookup[(control, quarter, attribute)] = verdict
    return lookup


def _format_attribute_definition(control_id: str, attribute_id: str) -> str:
    """Render the canonical ``"<control>.<attr> — <description>"`` string
    the judge expects in its ``attribute_definition`` prompt placeholder.

    Description is sourced from ``ATTRIBUTE_DEFINITIONS_PER_CONTROL``
    so the corpus TOC writer and the judge see byte-identical text.
    """
    description = ATTRIBUTE_DEFINITIONS_PER_CONTROL[control_id][attribute_id]
    return f"{control_id}.{attribute_id} — {description}"


def run_sweep(
    *,
    narratives: Iterable[AttributeNarrative],
    silver_reader: SilverEvidenceReader,
    judge: Judge,
    writer: Any,
    gold_lookup: dict[tuple[str, str, str], str],
    judge_run_id: str,
) -> tuple[int, dict[str, int], datetime, datetime]:
    """Iterate ``narratives``, judge each, write to ``gold.judge_outcomes``.

    Returns ``(n_total, verdict_counts, started_at, completed_at)``
    where ``verdict_counts`` always carries the three fixed keys
    ``pass`` / ``fail`` / ``uncertain`` (zero values for absent
    verdicts) so summary prints and downstream assertions never have
    to special-case missing keys.

    Silver evidence is cached per ``(engagement, control, quarter)``
    triple — 8 reads for the 32-narrative sweep, not 32 — since each
    triple's evidence covers every narratable attribute under it.

    Per-narrative exceptions are caught and logged; the sweep
    continues to the next narrative. The judge's own retry-and-fallback
    contract (task_03) means routine LLM-side failures are absorbed
    into ``verdict="uncertain"`` rows — only auth / network / writer
    failures surface here as exceptions.
    """
    started_at = datetime.now(UTC)
    n_total = 0
    verdict_counts: dict[str, int] = {"pass": 0, "fail": 0, "uncertain": 0}
    silver_cache: dict[tuple[str, str, str], ExtractedEvidence] = {}

    for narrative in narratives:
        try:
            cache_key = (
                narrative.engagement_id,
                narrative.control_id,
                narrative.quarter,
            )
            if cache_key not in silver_cache:
                silver_cache[cache_key] = silver_reader.read(*cache_key)
            evidence = silver_cache[cache_key]

            attribute_definition = _format_attribute_definition(
                narrative.control_id, narrative.attribute_id
            )
            gold_expected_verdict = gold_lookup[
                (narrative.control_id, narrative.quarter, narrative.attribute_id)
            ]

            response = judge.evaluate(
                narrative,
                evidence,
                gold_expected_verdict=gold_expected_verdict,
                attribute_definition=attribute_definition,
            )

            outcome = JudgeOutcomeRow(
                judge_run_id=judge_run_id,
                narrative_run_id=narrative.generation_run_id,
                engagement_id=narrative.engagement_id,
                control_id=narrative.control_id,
                attribute_id=narrative.attribute_id,
                quarter=narrative.quarter,
                judge_verdict=response.verdict,
                judge_confidence=response.confidence,
                judge_reasoning=response.reasoning,
                cited_evidence_fields=response.cited_evidence_fields,
                # FIXME(step_06_task_04 follow-up): derive judge_status from
                # Judge fallback signal once the judge exposes it; for now,
                # any returned JudgeResponse is treated as "ok" since the
                # current Judge swallows LLM failures into uncertain rows
                # without surfacing the status.
                judge_status="ok",
                gold_expected_verdict=gold_expected_verdict,
                fact_check_verdict="pass" if narrative.fact_check_passed else "fail",
                prompt_version=judge.prompt_version,
                model_deployment=judge.deployment,
                evaluated_at=datetime.now(UTC),
            )
            writer.write_judge_outcome(outcome)
            verdict_counts[response.verdict] += 1
            n_total += 1
        except Exception:
            # Per-row failures (auth, network, writer crash, etc.) must
            # not kill the sweep. The judge's own retry-and-fallback
            # already absorbs routine LLM-side failures into uncertain
            # rows; what reaches here is the residual unexpected.
            # Log + continue; failed rows are NOT counted in n_total
            # or verdict_counts, and NOT written to gold.judge_outcomes.
            logger.exception(
                "judge sweep failed for (%s, %s, %s, %s); skipping row",
                narrative.engagement_id,
                narrative.control_id,
                narrative.quarter,
                narrative.attribute_id,
            )

    completed_at = datetime.now(UTC)
    return n_total, verdict_counts, started_at, completed_at


def _new_run_id() -> str:
    """Mint a fresh per-sweep run id. 32 hex chars upper-cased — same
    shape as the generator's internal ``_new_run_id`` (run_layer2.py)
    so cost-telemetry joins across sweep families stay clean."""
    return secrets.token_hex(16).upper()


def main(argv: list[str] | None = None) -> int:
    """Operator entry point for the Step 6 task_04 judge sweep.

    Currently supports ``--dry-run`` only — the live sweep needs a
    ``GoldNarrativesReader`` (cycle D) to read the existing narratives
    out of ``audit_dev.gold.narratives``. Without it, this script
    cannot produce inputs for ``Judge.evaluate(...)``. Calling without
    ``--dry-run`` prints a clear pointer at the missing piece.

    ``--dry-run`` loads the gold-verdict lookup from
    ``eval/gold_scenarios/tocs/*.json`` and previews each of the 32
    narratable combinations with its gold expected verdict. No env-var
    auth, no LLM calls, no writes.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Layer 2 judge sweep — evaluate the 32 narratives in "
            "audit_dev.gold.narratives and write verdicts to "
            "audit_dev.gold.judge_outcomes for a single engagement."
        )
    )
    parser.add_argument(
        "--engagement-id",
        default="alpha-pension-fund-2025",
        help="Engagement to sweep (default: alpha-pension-fund-2025)",
    )
    parser.add_argument(
        "--toc-dir",
        default="eval/gold_scenarios/tocs",
        help=(
            "Directory containing the per-(control, quarter) ToC JSON "
            "files (default: eval/gold_scenarios/tocs)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the gold-verdict lookup summary + the 32 combinations "
            "that would be evaluated; no LLM calls, no writes."
        ),
    )
    args = parser.parse_args(argv)

    toc_dir = Path(args.toc_dir)
    gold_lookup = _load_gold_lookup_from_tocs(toc_dir)

    if not args.dry_run:
        # No half-implementations: live sweep wiring is explicit cycle D
        # work — pointer at the missing piece, non-zero exit.
        sys.stderr.write(
            "ERROR: live sweep wiring is not yet implemented "
            "(GoldNarrativesReader lands in cycle D). "
            "Use --dry-run to preview the gold lookup + narratable "
            "combinations.\n"
        )
        return 1

    print(f"✓ {len(gold_lookup)} gold-verdict entries loaded from {toc_dir}")
    print()
    combinations = list(iter_narratable_combinations(engagement_id=args.engagement_id))
    for _engagement, control, quarter, attribute in combinations:
        verdict = gold_lookup[(control, quarter, attribute)]
        print(f"  {control} {quarter} {attribute}: gold={verdict}")
    print()
    print(f"✓ {len(combinations)} combinations would be evaluated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
