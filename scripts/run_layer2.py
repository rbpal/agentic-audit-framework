"""Layer 2 full-sweep driver — iterate the 32 narratable combinations,
generate one narrative per combination, fact-check it, write it to
``audit_dev.gold.narratives``, and emit one cost-telemetry summary
row to ``audit_dev.gold.cost_telemetry``.

Composes the five Layer-2 primitives into a single sweep:

    silver evidence ── SilverEvidenceReader.read() ──── ExtractedEvidence
                                                              │
                                                              ▼
                       NarrativeGenerator.generate() ─── AttributeNarrative
                                ▲   (records token usage on UsageRecorder)
                                │
                                ▼
                       FactChecker.check() ───────────── FactCheckResult
                                                              │
                                                              ▼
              GoldNarrativeWriter.write_narrative() ──► gold.narratives row
                                                              │
                                                              ▼ (once at end)
              CostTelemetryWriter.write_cost_telemetry() ► gold.cost_telemetry row

For ``alpha-pension-fund-2025`` (the live dev engagement):

- DC-2 × {A, C, D} × {Q1-Q4} = 12 narratives
- DC-9 × {A, B, C, E, F} × {Q1-Q4} = 20 narratives
- Total: 32 narratives

Per-combination errors are caught and logged; the sweep continues
through the remaining combinations. Summary print at end shows
``N narratives generated; fact-check pass rate K/N`` plus the
cost-telemetry totals (tokens, USD, latency).

Usage::

    DATABRICKS_HOST=...                                              \\
    DATABRICKS_TOKEN=...                                             \\
    DATABRICKS_SQL_WAREHOUSE_ID=...                                  \\
    AZURE_OPENAI_ENDPOINT=https://aoai-aaf-rbpal-dev.openai.azure.com/ \\
    poetry run python scripts/run_layer2.py [--engagement-id ENG]
                                            [--prompt-version v1.0]
                                            [--dry-run]

Operator tool, not a test. CI runs unit tests only; this script
runs against live infrastructure on demand.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agentic_audit.layer2_narrative.cost_writer import CostTelemetryWriter
from agentic_audit.layer2_narrative.fact_checker import FactChecker
from agentic_audit.layer2_narrative.generator import NarrativeGenerator
from agentic_audit.layer2_narrative.gold_writer import GoldNarrativeWriter
from agentic_audit.layer2_narrative.silver_reader import SilverEvidenceReader
from agentic_audit.layer2_narrative.sweep import iter_narratable_combinations
from agentic_audit.models.evidence import ExtractedEvidence
from agentic_audit.models.narrative import (
    NarrativeResponse,
)
from agentic_audit.models.telemetry import (
    CostTelemetry,
    UsageRecorder,
    estimate_cost_usd,
)

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


def _build_warehouse_conn_factory() -> Any:
    """Wire ``databricks.sql.connect`` from env vars. Imported lazily
    so unit tests (which never call this) don't need the package
    installed."""
    required = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_SQL_WAREHOUSE_ID")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.stderr.write(
            f"ERROR: missing env vars: {', '.join(missing)}\n"
            "       set DATABRICKS_HOST, DATABRICKS_TOKEN, "
            "DATABRICKS_SQL_WAREHOUSE_ID and re-run.\n"
        )
        sys.exit(2)

    from databricks import sql as dbsql  # type: ignore[import-not-found]

    host = os.environ["DATABRICKS_HOST"].removeprefix("https://")
    http_path = f"/sql/1.0/warehouses/{os.environ['DATABRICKS_SQL_WAREHOUSE_ID']}"
    token = os.environ["DATABRICKS_TOKEN"]

    @contextmanager
    def factory() -> Generator[Any, None, None]:
        conn = dbsql.connect(
            server_hostname=host,
            http_path=http_path,
            access_token=token,
        )
        try:
            yield conn
        finally:
            conn.close()

    return factory


def run_sweep(
    *,
    engagement_id: str,
    prompt_version: str,
    silver_reader: SilverEvidenceReader,
    generator: NarrativeGenerator,
    fact_checker: FactChecker,
    gold_writer: GoldNarrativeWriter,
    generation_run_id: str,
) -> tuple[int, int, datetime, datetime]:
    """Iterate the 32 combinations, write one gold row per combination.

    Best-effort error handling: per-combination exceptions are logged
    and the sweep continues. Returns ``(n_total_succeeded,
    n_passed_fact_check, started_at, completed_at)`` — the timing
    pair is the wall-clock window for the whole sweep, used by the
    caller to build the ``CostTelemetry`` row.

    Silver evidence is cached per ``(engagement, control, quarter)``
    triple — 8 reads, not 32 — since each triple's evidence covers
    every narratable attribute under that triple.
    """
    silver_cache: dict[tuple[str, str, str], ExtractedEvidence] = {}
    n_succeeded = 0
    n_passed = 0
    started_at = datetime.now(UTC)

    for engagement, control, quarter, attribute in iter_narratable_combinations(
        engagement_id=engagement_id,
    ):
        t0 = time.perf_counter()
        try:
            cache_key = (engagement, control, quarter)
            if cache_key not in silver_cache:
                silver_cache[cache_key] = silver_reader.read(engagement, control, quarter)
            evidence = silver_cache[cache_key]

            narrative = generator.generate(
                attribute,
                evidence,
                generation_run_id=generation_run_id,
            )

            response_for_check = NarrativeResponse(
                narrative_text=narrative.narrative_text,
                cited_fields=narrative.cited_fields,
                word_count=narrative.word_count,
            )
            verdict = fact_checker.check(response_for_check, evidence)

            record = narrative.model_copy(
                update={
                    "fact_check_passed": verdict.passed,
                    "fact_check_issues": verdict.issues,
                }
            )
            gold_writer.write_narrative(record)

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            print(
                f"  {control} {quarter} {attribute}: "
                f"{narrative.word_count}w, "
                f"fact_check={'PASS' if verdict.passed else 'FAIL'} "
                f"({elapsed_ms:.0f} ms)"
            )
            n_succeeded += 1
            if verdict.passed:
                n_passed += 1
        except Exception:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            logger.exception(
                "sweep failed for (%s, %s, %s, %s) after %.0f ms",
                engagement,
                control,
                quarter,
                attribute,
                elapsed_ms,
            )
            print(f"  {control} {quarter} {attribute}: ERROR (see log) ({elapsed_ms:.0f} ms)")

    completed_at = datetime.now(UTC)
    return n_succeeded, n_passed, started_at, completed_at


def _new_run_id() -> str:
    """Mint a fresh per-sweep run id. 32 hex chars upper-cased — same
    shape as the generator's internal ``_new_run_id`` so cost
    telemetry follow-ups can join cleanly."""
    return secrets.token_hex(16).upper()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Layer 2 sweep — generate, fact-check, and write 32 narratives "
            "to audit_dev.gold.narratives for a single engagement."
        )
    )
    parser.add_argument(
        "--engagement-id",
        default="alpha-pension-fund-2025",
        help="Engagement to sweep (default: alpha-pension-fund-2025)",
    )
    parser.add_argument(
        "--prompt-version",
        default="v1.0",
        help="Prompt template version pinned at the generator (default: v1.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the 32 combinations and exit; no LLM calls, no writes.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        combos = list(iter_narratable_combinations(engagement_id=args.engagement_id))
        for engagement, control, quarter, attribute in combos:
            print(f"  {engagement} {control} {quarter} {attribute}")
        print(f"\n✓ dry run: {len(combos)} combinations would be processed")
        return 0

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    factory = _build_warehouse_conn_factory()
    silver_reader = SilverEvidenceReader(factory)
    recorder = UsageRecorder()
    generator = NarrativeGenerator.from_env(
        prompt_version=args.prompt_version,
        usage_recorder=recorder,
    )
    fact_checker = FactChecker()
    gold_writer = GoldNarrativeWriter(factory)
    cost_writer = CostTelemetryWriter(factory)
    run_id = _new_run_id()

    print(
        f"Layer 2 sweep: engagement={args.engagement_id}, "
        f"prompt_version={args.prompt_version}, run_id={run_id}"
    )
    n_total, n_passed, started_at, completed_at = run_sweep(
        engagement_id=args.engagement_id,
        prompt_version=args.prompt_version,
        silver_reader=silver_reader,
        generator=generator,
        fact_checker=fact_checker,
        gold_writer=gold_writer,
        generation_run_id=run_id,
    )

    telemetry = _build_cost_telemetry(
        agent_run_id=run_id,
        recorder=recorder,
        deployment=generator.deployment,
        started_at=started_at,
        completed_at=completed_at,
    )
    cost_writer.write_cost_telemetry(telemetry)

    print()
    print(f"✓ {n_total} narratives generated; fact-check pass rate {n_passed}/{n_total}")
    print(
        f"✓ cost telemetry: {recorder.n_calls} LLM calls, "
        f"{telemetry.input_tokens} prompt + {telemetry.output_tokens} completion = "
        f"{telemetry.total_tokens} tokens, "
        f"{telemetry.latency_ms} ms wall-clock, "
        f"cost_usd={telemetry.cost_usd}"
    )
    return 0


def _build_cost_telemetry(
    *,
    agent_run_id: str,
    recorder: UsageRecorder,
    deployment: str,
    started_at: datetime,
    completed_at: datetime,
) -> CostTelemetry:
    """Assemble one ``CostTelemetry`` row from the sweep state.

    ``model_version`` is set to the deployment name (e.g. ``"gpt-4o"``)
    rather than the snapshot version (e.g. ``"gpt-4o-2024-11-20"``);
    plumbing the snapshot version from each LLM response is a separate
    follow-up. ``cost_usd`` may be ``None`` when the deployment is
    absent from ``MODEL_PRICING_USD_PER_1K`` — log a WARN so the
    operator updates the price table.
    """
    snapshot = recorder.snapshot()
    cost_usd = estimate_cost_usd(
        deployment=deployment,
        input_tokens=snapshot.prompt_tokens,
        output_tokens=snapshot.completion_tokens,
    )
    if cost_usd is None:
        logger.warning(
            "deployment %r not in MODEL_PRICING_USD_PER_1K; "
            "cost_usd will be NULL in gold.cost_telemetry. "
            "Add a price row to src/agentic_audit/models/telemetry.py.",
            deployment,
        )
    latency_ms = int((completed_at - started_at).total_seconds() * 1000)
    return CostTelemetry(
        agent_run_id=agent_run_id,
        input_tokens=snapshot.prompt_tokens,
        output_tokens=snapshot.completion_tokens,
        total_tokens=snapshot.total_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        model_version=deployment,
        started_at=started_at,
        completed_at=completed_at,
    )


if __name__ == "__main__":
    sys.exit(main())
