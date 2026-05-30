"""Async worker — drains the Service Bus queue and runs investigations.

Two processing modes, picked by ``AAF_MOCK_BACKENDS``:

- **MOCK** (``AAF_MOCK_BACKENDS=1``): ``process_scenario`` sleeps a random
  ``MOCK_LATENCY_S`` (default 4-8s) and returns a synthetic result. No Azure
  OpenAI, no Databricks — so the scaling demo runs at high concurrency for
  $0 and the worker pods need no credentials. This is what exercises KEDA
  scale-from-queue-depth without burning the 10K-TPM quota.

- **REAL** (flag unset): wraps the existing ``run_investigation`` call
  sequence verbatim (the same one ``scripts/run_layer3_baseline.py``
  uses) — silver reads + the three sub-agents + cost/decisions writers.
  Used for the small, honest cost/latency burst.

``run_worker`` is the long-running consume loop (the container's worker
command). It builds the pipeline once at startup (REAL mode) so per-message
cost is just the investigation, not agent re-auth.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentic_audit.api.scenarios import SCENARIOS, Scenario

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


def _mock_enabled() -> bool:
    return os.getenv("AAF_MOCK_BACKENDS", "").strip().lower() in {"1", "true", "yes"}


def _mock_latency_bounds() -> tuple[float, float]:
    lo = float(os.getenv("MOCK_LATENCY_MIN_S", "4"))
    hi = float(os.getenv("MOCK_LATENCY_MAX_S", "8"))
    return (lo, hi) if lo <= hi else (hi, lo)


@dataclass
class ProcessResult:
    scenario_id: str
    mode: str  # "mock" | "real"
    recommendation: str
    status: str
    wall_clock_ms: int


# ── Warehouse wiring (mirrors scripts/run_layer3_baseline.py) ─────────


def _build_warehouse_conn_factory() -> Any:
    required = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_SQL_WAREHOUSE_ID")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"REAL mode needs env vars: {', '.join(missing)}")

    from databricks import sql as dbsql

    host = os.environ["DATABRICKS_HOST"].removeprefix("https://")
    http_path = f"/sql/1.0/warehouses/{os.environ['DATABRICKS_SQL_WAREHOUSE_ID']}"
    token = os.environ["DATABRICKS_TOKEN"]

    @contextmanager
    def factory() -> Generator[Any, None, None]:
        conn = dbsql.connect(server_hostname=host, http_path=http_path, access_token=token)
        try:
            yield conn
        finally:
            conn.close()

    return factory


class RealPipeline:
    """Holds the built-once pipeline objects for REAL-mode processing."""

    def __init__(self) -> None:
        from agentic_audit.layer2_narrative.cost_writer import CostTelemetryWriter
        from agentic_audit.layer2_narrative.silver_reader import SilverEvidenceReader
        from agentic_audit.layer3_agents.decisions_writer import Layer3DecisionsWriter
        from agentic_audit.layer3_agents.extraction_agent import ExtractionAgent
        from agentic_audit.layer3_agents.narrative_agent import NarrativeAgent
        from agentic_audit.layer3_agents.validation_agent import ValidationAgent

        factory = _build_warehouse_conn_factory()
        self.silver_reader = SilverEvidenceReader(factory)
        self.cost_writer = CostTelemetryWriter(factory)
        self.decisions_writer = Layer3DecisionsWriter(factory)
        self.extraction = ExtractionAgent.from_env()
        self.validation = ValidationAgent.from_env()
        self.narrative = NarrativeAgent.from_env()

    def run(self, scenario: Scenario, *, agent_run_id: str) -> ProcessResult:
        from agentic_audit.layer3_agents.supervisor import run_investigation
        from agentic_audit.models.evidence import AttributeCheck

        if scenario.prior_quarter is None:
            raise ValueError(f"{scenario.scenario_id} has no prior quarter; not REAL-eligible")

        current = self.silver_reader.read(
            engagement_id=scenario.engagement_id,
            control_id=scenario.control_id,
            quarter=scenario.quarter,
        )
        prior = self.silver_reader.read(
            engagement_id=scenario.engagement_id,
            control_id=scenario.control_id,
            quarter=scenario.prior_quarter,
        )
        attribute_id = "D" if scenario.control_id == "DC-9" else "B"
        check = AttributeCheck(
            control_id=scenario.control_id,  # type: ignore[arg-type]
            attribute_id=attribute_id,  # type: ignore[arg-type]
            status="fail",
        )

        started = time.monotonic()
        state = run_investigation(
            check,
            current,
            prior,
            agent_run_id=agent_run_id,
            extraction_agent=self.extraction,
            validation_agent=self.validation,
            narrative_agent=self.narrative,
            cost_writer=self.cost_writer,
            decisions_writer=self.decisions_writer,
        )
        wall_clock_ms = int((time.monotonic() - started) * 1000)
        fn = state.get("final_narrative")
        return ProcessResult(
            scenario_id=scenario.scenario_id,
            mode="real",
            recommendation=(fn.recommendation if fn is not None else "UNKNOWN"),
            status=str(state["status"]),
            wall_clock_ms=wall_clock_ms,
        )


def process_scenario(
    scenario_id: str,
    *,
    pipeline: RealPipeline | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ProcessResult:
    """Process one job. MOCK sleeps + returns synthetic; REAL runs the
    pipeline. ``sleep`` is injectable so tests don't actually wait."""
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise ValueError(f"unknown scenario_id {scenario_id!r}")

    if _mock_enabled() or pipeline is None:
        lo, hi = _mock_latency_bounds()
        latency_s = random.uniform(lo, hi)  # noqa: S311 - not security-sensitive
        started = time.monotonic()
        sleep(latency_s)
        wall_clock_ms = int((time.monotonic() - started) * 1000)
        logger.info("MOCK processed scenario=%s in %dms", scenario_id, wall_clock_ms)
        return ProcessResult(
            scenario_id=scenario_id,
            mode="mock",
            recommendation="MOCK",
            status="concluded",
            wall_clock_ms=wall_clock_ms,
        )

    agent_run_id = f"loadtest-{scenario_id}-{int(time.time() * 1000)}"
    return pipeline.run(scenario, agent_run_id=agent_run_id)


def run_worker() -> None:  # pragma: no cover - long-running loop, exercised live
    """Container worker entrypoint: consume the queue forever."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    mock = _mock_enabled()
    pipeline = None if mock else RealPipeline()
    logger.info("worker starting (mode=%s)", "mock" if mock else "real")

    connection_string = os.environ["SERVICEBUS_CONNECTION_STRING"]
    queue_name = os.getenv("SERVICEBUS_QUEUE_NAME", "investigations")

    from azure.servicebus import ServiceBusClient

    with ServiceBusClient.from_connection_string(connection_string) as client:
        receiver = client.get_queue_receiver(queue_name, max_wait_time=30)
        with receiver:
            for msg in receiver:
                try:
                    body = json.loads(str(msg))
                    process_scenario(body["scenario_id"], pipeline=pipeline)
                    receiver.complete_message(msg)
                except Exception:
                    logger.exception("message failed; abandoning for retry")
                    receiver.abandon_message(msg)


if __name__ == "__main__":  # pragma: no cover
    run_worker()
