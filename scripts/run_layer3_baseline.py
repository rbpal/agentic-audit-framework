"""Step 8 task_06 — Layer-3 v1 baseline sweep driver.

Operator-only script. Runs ``run_investigation`` across the
calibration scopes derived from the Step-5 corpus's ToC files
(``eval/gold_scenarios/tocs/dc*_Q*.json``), captures per-scope
outcomes, and prints aggregate baseline metrics (pass rate,
Wilson 95% CI, mean cost, p95 latency, token mix).

The baseline scope set is **4 scopes**, not the spec's
aspirational "5 known-good fixtures" — corpus survey on
2026-05-16 (privateDocs/step_08_agent_tools.md § Baseline)
showed only 4 meaningful exception scenarios exist for DC-9.D +
DC-2.B in the alpha-pension-fund-2025 fixtures:

  1. DC-9.D Q2 vs Q1 — rate change WITH amendment → ACCEPT
  2. DC-9.D Q3 vs Q2 — no rate change → ESCALATE fast-path
  3. DC-9.D Q4 vs Q3 — rate change WITHOUT amendment → ESCALATE
  4. DC-2.B Q4 vs Q3 — variance without explanation → ESCALATE

Predicted-vs-observed comparison is the load-bearing baseline
metric: how often does the agent's recommendation match what a
human auditor would conclude from the same evidence?

Env contract (same as scripts/verify_layer3_live.py):

  AZURE_OPENAI_ENDPOINT       https://aoai-aaf-rbpal-dev.openai.azure.com/
  DATABRICKS_HOST             https://adb-7405608712588657.17.azuredatabricks.net
  DATABRICKS_SQL_WAREHOUSE_ID dac9d7873e752cf0
  DATABRICKS_TOKEN            dapi... (PAT)

  poetry run python scripts/run_layer3_baseline.py

Cost: ~$0.10-0.15 total (4 investigations × ~$0.025-0.03 each).
Wall clock: ~60-90s end-to-end (silver reads dominate; LLM is
~8s per scope).

Idempotent — each scope mints a fresh investigation_run_id so
re-runs produce parallel rows in ``gold.layer3_decisions`` rather
than overwrites. The cost-telemetry table MERGEs on
``agent_run_id`` (one per sweep), so re-runs of the FULL sweep
under a fresh sweep id are safe.
"""

from __future__ import annotations

import logging
import math
import os
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentic_audit.layer2_narrative.cost_writer import CostTelemetryWriter
from agentic_audit.layer2_narrative.silver_reader import SilverEvidenceReader
from agentic_audit.layer3_agents.decisions_writer import Layer3DecisionsWriter
from agentic_audit.layer3_agents.extraction_agent import ExtractionAgent
from agentic_audit.layer3_agents.narrative_agent import NarrativeAgent
from agentic_audit.layer3_agents.supervisor import run_investigation
from agentic_audit.layer3_agents.validation_agent import ValidationAgent
from agentic_audit.models.engagement import ControlId, Quarter
from agentic_audit.models.evidence import AttributeCheck

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


# ── Baseline scope catalogue ──────────────────────────────────────────


@dataclass(frozen=True)
class BaselineScope:
    """One calibration scenario. ``predicted_recommendation`` is what
    a human auditor (or the gold ToC scenario) says the verdict
    should be; ``rationale`` documents WHY for the writeup."""

    label: str
    engagement_id: str
    control_id: ControlId
    quarter: Quarter
    prior_quarter: Quarter
    predicted_recommendation: str  # "ACCEPT" or "ESCALATE"
    rationale: str


BASELINE_SCOPES: list[BaselineScope] = [
    BaselineScope(
        label="dc9d_Q2vQ1_rate_change_with_amendment",
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-9",
        quarter="Q2",
        prior_quarter="Q1",
        predicted_recommendation="ACCEPT",
        rationale=(
            "ToC dc9_Q2.json defect=dc9_rate_change_with_amendment; DC-9.D "
            "expected=pass. The happy-path ACCEPT case: rate changed AND "
            "amendment exists to authorise. Agent should surface both and "
            "recommend ACCEPT."
        ),
    ),
    BaselineScope(
        label="dc9d_Q3vQ2_no_change",
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-9",
        quarter="Q3",
        prior_quarter="Q2",
        predicted_recommendation="ESCALATE",
        rationale=(
            "ToC dc9_Q3.json DC-9.D=pass (defect is on attribute C). No rate "
            "change in silver. Validation fast-path no-document → ESCALATE. "
            "Already run in PR #105; included here for sweep completeness."
        ),
    ),
    BaselineScope(
        label="dc9d_Q4vQ3_rate_change_no_amendment",
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-9",
        quarter="Q4",
        prior_quarter="Q3",
        predicted_recommendation="ESCALATE",
        rationale=(
            "ToC dc9_Q4.json defect=dc9_rate_change_without_amendment; "
            "DC-9.D=fail. Canonical SOX exception: rate changed but no "
            "amendment. Agent should surface 'NO AMENDMENT FILED' from "
            "silver notes/dict and recommend ESCALATE. Already run in "
            "PR #105 follow-up; included for sweep completeness."
        ),
    ),
    BaselineScope(
        label="dc2b_Q4vQ3_variance_no_explanation",
        engagement_id="alpha-pension-fund-2025",
        control_id="DC-2",
        quarter="Q4",
        prior_quarter="Q3",
        predicted_recommendation="ESCALATE",
        rationale=(
            "ToC dc2_Q4.json defect=dc2_variance_no_explanation; "
            "DC-2.B=fail. The DC-2.B equivalent of the SOX exception: "
            "variance flagged but no reviewer explanation. Agent should "
            "surface the empty/missing explanation and recommend ESCALATE."
        ),
    ),
]


# ── Warehouse wiring (mirrors verify_layer3_live.py) ─────────────────


def _build_warehouse_conn_factory() -> Any:
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


# ── Per-scope result + aggregate ──────────────────────────────────────


@dataclass
class ScopeResult:
    scope: BaselineScope
    agent_recommendation: str
    status: str
    final_verdict: str
    investigation_run_id: str
    narrative_text: str
    iterations_used: int
    wall_clock_ms: int

    @property
    def matches_predicted(self) -> bool:
        return self.agent_recommendation == self.scope.predicted_recommendation


def _wilson_ci(passes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI on a proportion. Same statistic Step 5 task_08
    uses for the calibrated 27/27 baseline."""
    if total == 0:
        return (0.0, 0.0)
    p = passes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    # Nearest-rank method on small N — sufficient for a 4-row sample.
    idx = max(0, int(math.ceil(0.95 * len(sorted_v))) - 1)
    return sorted_v[idx]


# ── Cost-row fetch for aggregation ────────────────────────────────────


def _fetch_cost_rows(factory: Any, agent_run_id: str) -> list[dict[str, Any]]:
    """Pull all cost_telemetry rows for the sweep's agent_run_id.

    Note: today the CostTelemetryWriter MERGEs on agent_run_id, so
    each investigation in the sweep OVERWRITES the prior cost row.
    For the baseline writeup we need per-investigation costs, so this
    sweep gives each investigation its OWN agent_run_id (sweep_id +
    scope label suffix) — see main()."""
    sql = """
SELECT agent_run_id, input_tokens, output_tokens, total_tokens,
       latency_ms, cost_usd
FROM audit_dev.gold.cost_telemetry
WHERE agent_run_id LIKE %(prefix)s || '%%'
"""
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, {"prefix": agent_run_id})
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


# ── Output formatting ─────────────────────────────────────────────────


def _print_scope_summary(idx: int, total: int, scope: BaselineScope) -> None:
    print(f"\n{'=' * 72}")
    print(f"Scope {idx}/{total}: {scope.label}")
    print(
        f"  {scope.engagement_id} / {scope.control_id} / {scope.quarter} vs {scope.prior_quarter}"
    )
    print(f"  Predicted recommendation: {scope.predicted_recommendation}")
    print(f"  Rationale: {scope.rationale}")


def _print_scope_result(result: ScopeResult) -> None:
    match = "✓ match" if result.matches_predicted else "✗ MISMATCH"
    print(f"\n  Investigation run id : {result.investigation_run_id}")
    print(f"  Status               : {result.status}")
    print(f"  Final verdict        : {result.final_verdict}")
    print(f"  Recommendation       : {result.agent_recommendation}  [{match}]")
    print(f"  Iterations           : {result.iterations_used}")
    print(f"  Wall clock           : {result.wall_clock_ms} ms")
    # Print first 300 chars of narrative — full body is in gold.
    preview = result.narrative_text[:300]
    if len(result.narrative_text) > 300:
        preview += "..."
    print(f"  Narrative (preview)  :\n    {preview}")


def _print_aggregate(
    sweep_agent_run_id: str,
    results: list[ScopeResult],
    cost_rows: list[dict[str, Any]],
) -> None:
    total = len(results)
    matches = sum(1 for r in results if r.matches_predicted)
    pass_rate = matches / total if total else 0.0
    ci_lo, ci_hi = _wilson_ci(matches, total)

    wall_clock_values = [float(r.wall_clock_ms) for r in results]
    cost_values = [float(row.get("cost_usd") or 0.0) for row in cost_rows]
    token_in = sum(int(row.get("input_tokens") or 0) for row in cost_rows)
    token_out = sum(int(row.get("output_tokens") or 0) for row in cost_rows)
    token_total = sum(int(row.get("total_tokens") or 0) for row in cost_rows)
    latency_values = [float(row.get("latency_ms") or 0) for row in cost_rows]

    print(f"\n{'=' * 72}")
    print(f"=== Layer-3 v1 baseline — sweep_agent_run_id={sweep_agent_run_id} ===")
    print(f"{'=' * 72}")
    print(f"\nScopes run            : {total}")
    print(f"Predicted-matches     : {matches}/{total}")
    print(f"Pass rate             : {pass_rate:.1%}")
    print(f"Wilson 95% CI         : [{ci_lo:.1%}, {ci_hi:.1%}]")
    print()
    print(f"Total cost (USD)      : ${sum(cost_values):.4f}")
    print(f"Mean cost / scope     : ${(sum(cost_values) / total if total else 0):.4f}")
    print(f"p95 LLM latency (ms)  : {_p95(latency_values):.0f}")
    print(f"Mean wall clock (ms)  : {(sum(wall_clock_values) / total if total else 0):.0f}")
    print()
    print(f"Tokens — input        : {token_in}")
    print(f"Tokens — output       : {token_out}")
    print(f"Tokens — total        : {token_total}")
    print()
    print("Per-scope verdicts (for the writeup):")
    for r in results:
        match = "match" if r.matches_predicted else "MISMATCH"
        print(
            f"  {r.scope.label:50s}  predicted={r.scope.predicted_recommendation:8s}  "
            f"observed={r.agent_recommendation:8s}  [{match}]"
        )


# ── Main ──────────────────────────────────────────────────────────────


def _run_one_scope(
    *,
    scope: BaselineScope,
    sweep_agent_run_id: str,
    factory: Any,
    silver_reader: SilverEvidenceReader,
    extraction: ExtractionAgent,
    validation: ValidationAgent,
    narrative: NarrativeAgent,
    cost_writer: CostTelemetryWriter,
    decisions_writer: Layer3DecisionsWriter,
) -> ScopeResult:
    """Run ``run_investigation`` for one scope, capture the result."""
    # Per-scope agent_run_id so cost_telemetry rows don't collide on
    # MERGE (the writer keys on agent_run_id). Format keeps the sweep
    # id as a prefix for easy SELECT-all-rows-from-this-sweep queries.
    per_scope_agent_run_id = f"{sweep_agent_run_id}-{scope.label}"
    current_evidence = silver_reader.read(
        engagement_id=scope.engagement_id,
        control_id=scope.control_id,
        quarter=scope.quarter,
    )
    prior_evidence = silver_reader.read(
        engagement_id=scope.engagement_id,
        control_id=scope.control_id,
        quarter=scope.prior_quarter,
    )
    attribute_id = "D" if scope.control_id == "DC-9" else "B"
    check = AttributeCheck(
        control_id=scope.control_id,
        attribute_id=attribute_id,  # type: ignore[arg-type]
        status="fail",
    )

    started = time.monotonic()
    final_state = run_investigation(
        check,
        current_evidence,
        prior_evidence,
        agent_run_id=per_scope_agent_run_id,
        extraction_agent=extraction,
        validation_agent=validation,
        narrative_agent=narrative,
        cost_writer=cost_writer,
        decisions_writer=decisions_writer,
    )
    wall_clock_ms = int((time.monotonic() - started) * 1000)

    fn = final_state.get("final_narrative")
    return ScopeResult(
        scope=scope,
        agent_recommendation=(fn.recommendation if fn is not None else "UNKNOWN"),
        status=str(final_state["status"]),
        final_verdict=str(final_state.get("status", "uncertain")),  # status maps to verdict in v1
        investigation_run_id=str(final_state["investigation_run_id"]),
        narrative_text=(fn.narrative_text if fn is not None else ""),
        iterations_used=int(final_state.get("iterations_used", 0)),
        wall_clock_ms=wall_clock_ms,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,  # Quieter than verify_layer3_live to keep per-scope output readable
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not os.getenv("AZURE_OPENAI_ENDPOINT"):
        sys.stderr.write(
            "ERROR: AZURE_OPENAI_ENDPOINT not set. "
            "Export e.g. https://aoai-aaf-rbpal-dev.openai.azure.com/ and re-run.\n"
        )
        return 2

    factory = _build_warehouse_conn_factory()
    silver_reader = SilverEvidenceReader(factory)
    cost_writer = CostTelemetryWriter(factory)
    decisions_writer = Layer3DecisionsWriter(factory)

    # Reuse one set of agents across all scopes — cuts auth setup cost.
    extraction = ExtractionAgent.from_env()
    validation = ValidationAgent.from_env()
    narrative = NarrativeAgent.from_env()

    sweep_agent_run_id = f"baseline-{secrets.token_hex(6).upper()}"
    print("\n=== Layer-3 v1 baseline sweep ===")
    print(f"  sweep_agent_run_id  : {sweep_agent_run_id}")
    print(f"  endpoint            : {os.environ['AZURE_OPENAI_ENDPOINT']}")
    print(f"  warehouse           : {os.environ['DATABRICKS_HOST']}")
    print(f"  scopes              : {len(BASELINE_SCOPES)}")

    results: list[ScopeResult] = []
    failures: list[tuple[BaselineScope, Exception]] = []
    for idx, scope in enumerate(BASELINE_SCOPES, start=1):
        _print_scope_summary(idx, len(BASELINE_SCOPES), scope)
        try:
            result = _run_one_scope(
                scope=scope,
                sweep_agent_run_id=sweep_agent_run_id,
                factory=factory,
                silver_reader=silver_reader,
                extraction=extraction,
                validation=validation,
                narrative=narrative,
                cost_writer=cost_writer,
                decisions_writer=decisions_writer,
            )
        except Exception as exc:
            # Don't let one scope's failure kill the sweep — capture
            # and continue so the operator gets partial baseline data.
            print(f"\n  EXCEPTION: {type(exc).__name__}: {exc}")
            failures.append((scope, exc))
            continue
        results.append(result)
        _print_scope_result(result)

    if failures:
        print(f"\n{'!' * 72}")
        print(f"!! {len(failures)} scope(s) failed — partial baseline below")
        for scope, exc in failures:
            print(f"!!   {scope.label}: {exc}")
        print(f"{'!' * 72}")

    cost_rows = _fetch_cost_rows(factory, sweep_agent_run_id)
    _print_aggregate(sweep_agent_run_id, results, cost_rows)

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
