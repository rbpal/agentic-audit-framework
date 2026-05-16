"""Operator-only live verification of the Step 7 Layer-3 pipeline.

Builds the 4 sub-agents + 2 gold-table writers against the dev
Azure OpenAI deployment and Databricks SQL warehouse, runs one
investigation against a synthetic DC-9.D Q3 scope, then SELECTs
both gold rows to prove the writers landed.

Env contract (same as scripts/run_layer2.py):

  AZURE_OPENAI_ENDPOINT       e.g. https://aoai-aaf-rbpal-dev.openai.azure.com/
  DATABRICKS_HOST             e.g. https://adb-7405608712588657.17.azuredatabricks.net
  DATABRICKS_SQL_WAREHOUSE_ID e.g. dac9d7873e752cf0
  DATABRICKS_TOKEN            dapi... (PAT)

  poetry run python scripts/verify_layer3_live.py

Cost: ~$0.05-0.10 (the supervisor's happy path runs ~10-15 LLM
calls aggregated across extraction + validation + narrative + judge).

Idempotent for cost_telemetry (MERGE on agent_run_id when re-run
with the same id; this driver mints a fresh id per run so each
verification produces a new row pair).
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agentic_audit.layer2_narrative.cost_writer import CostTelemetryWriter
from agentic_audit.layer3_agents.decisions_writer import Layer3DecisionsWriter
from agentic_audit.layer3_agents.extraction_agent import ExtractionAgent
from agentic_audit.layer3_agents.narrative_agent import NarrativeAgent
from agentic_audit.layer3_agents.supervisor import run_investigation
from agentic_audit.layer3_agents.validation_agent import ValidationAgent
from agentic_audit.models.evidence import (
    ATTRIBUTES_PER_CONTROL,
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

UTC_TS = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
ENG_ID = "eng-live-verify"


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


def _evidence(control_id: str, quarter: str) -> ExtractedEvidence:
    """Synthetic DC-9 evidence — matches the live extraction test
    pattern. Real silver evidence is not needed here; the placeholder
    extraction tools return canned empty payloads regardless (Step 8
    wires real bodies). Goal is verifying writer wiring, not tool
    correctness."""
    return ExtractedEvidence(
        engagement_id=ENG_ID,
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id=f"run-{quarter}",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="JD", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="MR", role="reviewer", date=UTC_TS),
        attributes=[
            AttributeCheck(
                control_id=control_id,  # type: ignore[arg-type]
                attribute_id=a,  # type: ignore[arg-type]
                status="pass",
            )
            for a in ATTRIBUTES_PER_CONTROL[control_id]
        ],
        source_bronze_file_hash="abc",
    )


def _new_agent_run_id() -> str:
    return f"verify-{secrets.token_hex(8).upper()}"


def _print_decision_row(factory: Any, investigation_run_id: str) -> None:
    sql = """
SELECT
    investigation_run_id, agent_run_id, engagement_id, control_id,
    attribute_id, quarter, exception_type, final_verdict,
    final_confidence, iterations_used, status, recommendation,
    judge_verdict, judge_confidence, prompt_version, model_deployment,
    decided_at, LEFT(narrative_text, 240) AS narrative_preview
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if not os.getenv("AZURE_OPENAI_ENDPOINT"):
        sys.stderr.write(
            "ERROR: AZURE_OPENAI_ENDPOINT not set. "
            "Export e.g. https://aoai-aaf-rbpal-dev.openai.azure.com/ and re-run.\n"
        )
        return 2

    factory = _build_warehouse_conn_factory()
    cost_writer = CostTelemetryWriter(factory)
    decisions_writer = Layer3DecisionsWriter(factory)

    extraction = ExtractionAgent.from_env()
    validation = ValidationAgent.from_env()
    narrative = NarrativeAgent.from_env()

    agent_run_id = _new_agent_run_id()
    check = AttributeCheck(control_id="DC-9", attribute_id="D", status="fail")

    print(f"\n=== Live verification — agent_run_id={agent_run_id} ===")
    print(f"    endpoint={os.environ['AZURE_OPENAI_ENDPOINT']}")
    print(f"    warehouse={os.environ['DATABRICKS_HOST']}")
    print("    judge: None (fail-closed escalate; production judge wiring deferred)")
    print()

    final_state = run_investigation(
        check,
        _evidence("DC-9", "Q3"),
        _evidence("DC-9", "Q2"),
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
