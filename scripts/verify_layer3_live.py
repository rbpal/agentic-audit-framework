"""Operator-only live verification of the Step 7 Layer-3 pipeline,
now against real silver evidence (Step 8 task_05).

Builds the 3 sub-agents + 2 gold-table writers against the dev
Azure OpenAI deployment and Databricks SQL warehouse, loads
``ExtractedEvidence`` for the requested scope via
``SilverEvidenceReader``, runs one investigation, then SELECTs both
gold rows to prove the writers landed AND surface what the agent
actually extracted.

Env contract (same as scripts/run_layer2.py):

  AZURE_OPENAI_ENDPOINT       e.g. https://aoai-aaf-rbpal-dev.openai.azure.com/
  DATABRICKS_HOST             e.g. https://adb-7405608712588657.17.azuredatabricks.net
  DATABRICKS_SQL_WAREHOUSE_ID e.g. dac9d7873e752cf0
  DATABRICKS_TOKEN            dapi... (PAT)

CLI:

  poetry run python scripts/verify_layer3_live.py
  poetry run python scripts/verify_layer3_live.py \
      --engagement-id alpha-pension-fund-2025 \
      --control-id DC-9 --quarter Q3 --prior-quarter Q2

Defaults match the Step 5 calibrated corpus
(``alpha-pension-fund-2025``, DC-9 Q3 vs Q2).

Cost: ~$0.05-0.10 (the supervisor's happy path runs 6-10 LLM calls
across extraction + validation + narrative; real tool returns tend
to extend the ReAct loop slightly vs the placeholder-empty case in
PR #100).

Idempotent for cost_telemetry (MERGE on agent_run_id when re-run
with the same id; this driver mints a fresh id per run so each
verification produces a new row pair).

What changed vs PR #100
-----------------------

PR #100 used synthetic ``ExtractedEvidence`` because the tools
returned empty payloads anyway — the narrative read "rate delta of
'unknown -> unknown'". Step 8 tasks 01-03 swapped the tool bodies
for real ones that read state via ``InjectedState``. Task_05 now
flows real silver evidence into that state, so the agent's tool
calls return real rates + (best-effort) amendment text, and the
narrative cites them.

**Diagnostic for "Step 8 closed"** (per ``privateDocs/step_08_agent_tools.md``):
the narrative-text preview in ``gold.layer3_decisions`` must mention
actual rate numbers (e.g., ``"28.5 → 30.0"``) instead of
``"unknown -> unknown"``.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from agentic_audit.layer2_narrative.cost_writer import CostTelemetryWriter
from agentic_audit.layer2_narrative.silver_reader import SilverEvidenceReader
from agentic_audit.layer3_agents.decisions_writer import Layer3DecisionsWriter
from agentic_audit.layer3_agents.extraction_agent import ExtractionAgent
from agentic_audit.layer3_agents.narrative_agent import NarrativeAgent
from agentic_audit.layer3_agents.supervisor import run_investigation
from agentic_audit.layer3_agents.validation_agent import ValidationAgent
from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import AttributeCheck, ExtractedEvidence

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


# ── Warehouse + silver wiring ─────────────────────────────────────────


def _build_warehouse_conn_factory() -> Any:
    """Mirror of scripts/run_layer2.py._build_warehouse_conn_factory."""
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


def _load_silver_evidence(
    reader: SilverEvidenceReader,
    *,
    engagement_id: str,
    control_id: ControlId,
    quarter: Quarter,
) -> ExtractedEvidence:
    """Load real ``ExtractedEvidence`` from silver. Raises
    ``SilverReadError`` (from the reader) if the scope isn't
    populated — surfaces loudly rather than silently substituting an
    empty payload, because the verification's whole point is to
    confirm real data flows through."""
    return reader.read(engagement_id=engagement_id, control_id=control_id, quarter=quarter)


# ── Output helpers ────────────────────────────────────────────────────


def _new_agent_run_id() -> str:
    return f"verify-{secrets.token_hex(8).upper()}"


def _print_decision_row(factory: Any, investigation_run_id: str) -> None:
    sql = """
SELECT
    investigation_run_id, agent_run_id, engagement_id, control_id,
    attribute_id, quarter, exception_type, final_verdict,
    final_confidence, iterations_used, status, recommendation,
    judge_verdict, judge_confidence, prompt_version, model_deployment,
    decided_at, LEFT(narrative_text, 400) AS narrative_preview
FROM audit_dev.gold.layer3_decisions
WHERE investigation_run_id = %(irid)s
"""
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, {"irid": investigation_run_id})
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    print("\n=== gold.layer3_decisions ===")
    if not rows:
        print(f"  (no row found for investigation_run_id={investigation_run_id})")
        return
    for r in rows:
        for c, v in zip(cols, r, strict=True):
            print(f"  {c:>22}: {v}")


def _print_cost_row(factory: Any, agent_run_id: str) -> None:
    sql = """
SELECT agent_run_id, input_tokens, output_tokens, total_tokens,
       latency_ms, cost_usd, model_version, started_at, completed_at
FROM audit_dev.gold.cost_telemetry
WHERE agent_run_id = %(arid)s
"""
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, {"arid": agent_run_id})
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    print("\n=== gold.cost_telemetry ===")
    if not rows:
        print(f"  (no row found for agent_run_id={agent_run_id})")
        return
    for r in rows:
        for c, v in zip(cols, r, strict=True):
            print(f"  {c:>14}: {v}")


# ── CLI + main ────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Layer-3 live verification driver — runs one investigation "
            "against real silver evidence for the requested scope."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Defaults match the Step 5 calibrated corpus's main engagement "
            "(alpha-pension-fund-2025, DC-9 Q3 vs Q2). Pass --scope flags "
            "to verify against a different engagement / control / period."
        ),
    )
    parser.add_argument(
        "--engagement-id",
        default="alpha-pension-fund-2025",
        help="Engagement identifier loaded into silver (default: %(default)s).",
    )
    parser.add_argument(
        "--control-id",
        default="DC-9",
        choices=("DC-2", "DC-9"),
        help="SOX control under investigation (default: %(default)s).",
    )
    parser.add_argument(
        "--quarter",
        default="Q3",
        choices=("Q1", "Q2", "Q3", "Q4"),
        help="Period under investigation (default: %(default)s).",
    )
    parser.add_argument(
        "--prior-quarter",
        default="Q2",
        choices=("Q1", "Q2", "Q3", "Q4"),
        help="Comparison period for cross-quarter reasoning (default: %(default)s).",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args()

    if not os.getenv("AZURE_OPENAI_ENDPOINT"):
        sys.stderr.write(
            "ERROR: AZURE_OPENAI_ENDPOINT not set. "
            "Export e.g. https://aoai-aaf-rbpal-dev.openai.azure.com/ and re-run.\n"
        )
        return 2

    factory = _build_warehouse_conn_factory()
    cost_writer = CostTelemetryWriter(factory)
    decisions_writer = Layer3DecisionsWriter(factory)
    silver_reader = SilverEvidenceReader(factory)

    extraction = ExtractionAgent.from_env()
    validation = ValidationAgent.from_env()
    narrative = NarrativeAgent.from_env()

    agent_run_id = _new_agent_run_id()
    # The attribute the supervisor routes on is fixed per exception
    # type (DC-9.D billing-rate-change uses attribute D; DC-2.B
    # variance uses attribute B). Hardcoded here because the
    # supervisor's Layer-1 trigger already makes the same mapping;
    # a future per-control map lifts both.
    attribute_id = "D" if args.control_id == "DC-9" else "B"
    check = AttributeCheck(
        control_id=cast(ControlId, args.control_id),
        attribute_id=attribute_id,  # type: ignore[arg-type]
        status="fail",
    )

    print(f"\n=== Live verification — agent_run_id={agent_run_id} ===")
    print(f"    endpoint   : {os.environ['AZURE_OPENAI_ENDPOINT']}")
    print(f"    warehouse  : {os.environ['DATABRICKS_HOST']}")
    print(
        f"    scope      : engagement={args.engagement_id} "
        f"control={args.control_id} quarter={args.quarter} "
        f"prior={args.prior_quarter}"
    )
    print("    judge      : None (fail-closed escalate; production judge wiring deferred)")
    print()

    print(f"Loading silver evidence for {args.engagement_id} / {args.control_id} ...")
    current_evidence = _load_silver_evidence(
        silver_reader,
        engagement_id=args.engagement_id,
        control_id=cast(ControlId, args.control_id),
        quarter=cast(Quarter, args.quarter),
    )
    prior_evidence = _load_silver_evidence(
        silver_reader,
        engagement_id=args.engagement_id,
        control_id=cast(ControlId, args.control_id),
        quarter=cast(Quarter, args.prior_quarter),
    )

    # Surface the loaded evidence's target-attribute slice so the
    # operator can see what the tool will project from. This is the
    # ground truth the narrative is compared against.
    print(f"\n=== Silver evidence: {args.control_id}.{attribute_id} current quarter ===")
    target = next(
        (a for a in current_evidence.attributes if a.attribute_id == attribute_id),
        None,
    )
    if target is None:
        print(f"  (attribute {attribute_id!r} not present in silver evidence)")
    else:
        print(f"  status              : {target.status}")
        print(f"  extracted_value     : {target.extracted_value!r}")
        print(f"  evidence_cell_refs  : {target.evidence_cell_refs}")
        print(f"  notes               : {target.notes!r}")

    final_state = run_investigation(
        check,
        current_evidence,
        prior_evidence,
        agent_run_id=agent_run_id,
        extraction_agent=extraction,
        validation_agent=validation,
        narrative_agent=narrative,
        cost_writer=cost_writer,
        decisions_writer=decisions_writer,
    )

    investigation_run_id = final_state["investigation_run_id"]
    print("\n=== Investigation terminal state ===")
    print(f"  investigation_run_id : {investigation_run_id}")
    print(f"  status               : {final_state['status']}")
    print(f"  iterations_used      : {final_state['iterations_used']}")
    print(f"  confidence_score     : {final_state['confidence_score']:.3f}")
    print(f"  judge_verdict        : {final_state.get('judge_verdict')!r}")
    fn = final_state.get("final_narrative")
    if fn is not None:
        print(f"  recommendation       : {fn.recommendation}")
        print(f"  word_count           : {fn.word_count}")

    _print_decision_row(factory, investigation_run_id)
    _print_cost_row(factory, agent_run_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
