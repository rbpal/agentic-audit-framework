"""Layer 1 full-sweep driver — repopulate `audit_dev.silver.evidence`
across the 8 v2 corpus scenarios (DC-2 + DC-9 × Q1–Q4).

Built for the manual cloud step that follows
`step_05_task_02a_extend_silver_evidence_schema`: after Terraform
adds the envelope columns to `silver.evidence`, this script re-extracts
all 8 (engagement, control, quarter) triples from bronze and writes
them back to silver via `SilverWriter.write_evidence(...)`. Idempotent
under MERGE — re-running is safe.

Also useful any time silver needs full repopulation (schema migration,
silver_writer logic change, corpus refresh).

Usage::

    DATABRICKS_HOST=...                                              \\
    DATABRICKS_TOKEN=...                                             \\
    DATABRICKS_SQL_WAREHOUSE_ID=...                                  \\
    poetry run python scripts/run_layer1.py [--engagement ENG]
                                            [--scenarios dc2_q1,dc9_q3,...]

By default sweeps all 8 v2 scenarios for engagement
``alpha-pension-fund-2025``. Pass ``--scenarios`` with a comma-separated
list of ``dc{2|9}_q{1|2|3|4}`` tags to limit the sweep.

This is an operator tool, not a test. CI runs unit tests only; this
script is on-demand.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from agentic_audit.layer1_extract.bronze_reader import BronzeReader
from agentic_audit.layer1_extract.orchestrator import extract
from agentic_audit.layer1_extract.silver_writer import SilverWriter
from agentic_audit.models.engagement import ControlId, Quarter

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable


SCENARIOS: tuple[tuple[ControlId, Quarter], ...] = (
    ("DC-2", "Q1"),
    ("DC-2", "Q2"),
    ("DC-2", "Q3"),
    ("DC-2", "Q4"),
    ("DC-9", "Q1"),
    ("DC-9", "Q2"),
    ("DC-9", "Q3"),
    ("DC-9", "Q4"),
)


def parse_scenarios(arg: str | None) -> tuple[tuple[ControlId, Quarter], ...]:
    """Parse a comma-separated ``dc{2|9}_q{1-4}`` list into typed tuples.

    ``None`` or an empty string returns the full default sweep.
    """
    if not arg:
        return SCENARIOS
    out: list[tuple[ControlId, Quarter]] = []
    for tag in (s.strip() for s in arg.split(",") if s.strip()):
        if "_" not in tag:
            raise ValueError(f"bad scenario tag {tag!r}; expected like 'dc9_q1'")
        ctrl_part, q_part = tag.split("_", 1)
        ctrl = f"DC-{ctrl_part.removeprefix('dc')}"
        quarter = q_part.upper()
        if ctrl not in ("DC-2", "DC-9") or quarter not in ("Q1", "Q2", "Q3", "Q4"):
            raise ValueError(f"bad scenario tag {tag!r}; control={ctrl!r}, quarter={quarter!r}")
        out.append((ctrl, quarter))  # type: ignore[arg-type]
    return tuple(out)


def run_sweep(
    *,
    engagement_id: str,
    bronze_reader: BronzeReader,
    silver_writer: SilverWriter,
    scenarios: Iterable[tuple[ControlId, Quarter]] = SCENARIOS,
) -> int:
    """Iterate scenarios, extract from bronze, write to silver. Returns
    the total number of silver rows written (4 per DC-2, 6 per DC-9).
    """
    total_rows = 0
    for control_id, quarter in scenarios:
        t0 = time.perf_counter()
        record = extract(engagement_id, control_id, quarter, bronze_reader=bronze_reader)
        silver_writer.write_evidence(record)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        total_rows += len(record.attributes)
        print(
            f"  {control_id} {quarter}: wrote {len(record.attributes)} silver rows "
            f"in {elapsed_ms:.1f} ms (run_id={record.run_id})"
        )
    return total_rows


def _build_conn_factory() -> Any:
    """Wire `databricks.sql.connect` from env vars. Imported lazily so
    unit tests (which never call this) don't need the package installed.
    """
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Repopulate audit_dev.silver.evidence by re-extracting all 8 v2 scenarios from bronze."
        )
    )
    parser.add_argument(
        "--engagement",
        default="alpha-pension-fund-2025",
        help="engagement_id to extract (default: alpha-pension-fund-2025)",
    )
    parser.add_argument(
        "--scenarios",
        default=None,
        help=("comma-separated dc{2|9}_q{1-4} list to limit the sweep (default: all 8 scenarios)"),
    )
    args = parser.parse_args(argv)

    scenarios = parse_scenarios(args.scenarios)

    factory = _build_conn_factory()
    bronze_reader = BronzeReader(factory)
    silver_writer = SilverWriter(factory)

    print(
        f"Layer 1 sweep: engagement={args.engagement}, "
        f"scenarios={[f'{c}-{q}' for c, q in scenarios]}"
    )
    total_rows = run_sweep(
        engagement_id=args.engagement,
        bronze_reader=bronze_reader,
        silver_writer=silver_writer,
        scenarios=scenarios,
    )
    print()
    print(f"✓ wrote {total_rows} silver rows across {len(scenarios)} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
