"""Performance benchmark for Layer 1 (`task_06`).

Measures wall time of `extract(engagement, control, quarter)` across
the 8 v2 corpus scenarios (DC-2 + DC-9 × Q1–Q4) on a live Databricks
SQL warehouse and reports P50 / P95 / P99 latencies plus total elapsed.

**Target:** P95 < 600 ms per `(engagement, control, quarter)` extraction
on the Databricks Serverless Starter Warehouse.

Usage::

    DATABRICKS_HOST=...                                              \\
    DATABRICKS_TOKEN=...                                             \\
    DATABRICKS_SQL_WAREHOUSE_ID=...                                  \\
    poetry run python scripts/benchmark_layer1.py [--rounds N]
                                                  [--engagement ENG]
                                                  [--json-out PATH]

`--rounds N` repeats the 8-scenario sweep N times (default 1) so P95 /
P99 are computed over more samples. Useful for warm-cache vs cold-cache
comparisons.

`--json-out PATH` writes the per-call timings as JSON to PATH for
downstream analysis. By default the script just prints the summary line
that gets pasted into `privateDocs/benchmarks.md`.

The script is *not* part of the test suite — it's an operator tool. CI
runs unit tests; the benchmark is on-demand.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agentic_audit.layer1_extract.bronze_reader import BronzeReader
from agentic_audit.layer1_extract.orchestrator import extract
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


@dataclass(frozen=True)
class Sample:
    engagement_id: str
    control_id: str
    quarter: str
    duration_ms: float


@dataclass(frozen=True)
class Stats:
    n: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    total_ms: float
    mean_ms: float


def percentile(sorted_values: list[float], p: float) -> float:
    """Inclusive nearest-rank percentile.

    `p` is in [0, 1]. The nearest-rank method picks
    ``ceil(p * n)``-th value (1-indexed). Stable for small n; matches
    the spec doc's `sorted(durations_ms)[int(p * n)]` snippet.
    """
    if not sorted_values:
        raise ValueError("cannot compute percentile of an empty list")
    n = len(sorted_values)
    rank = max(0, min(n - 1, int(p * n)))
    return sorted_values[rank]


def summarise(samples: list[Sample]) -> Stats:
    if not samples:
        raise ValueError("cannot summarise an empty sample list")
    durations = sorted(s.duration_ms for s in samples)
    return Stats(
        n=len(durations),
        p50_ms=statistics.median(durations),
        p95_ms=percentile(durations, 0.95),
        p99_ms=percentile(durations, 0.99),
        total_ms=sum(durations),
        mean_ms=statistics.fmean(durations),
    )


def format_summary_line(stats: Stats, *, sku: str) -> str:
    """One-line markdown row for `privateDocs/benchmarks.md`."""
    return (
        f"| {datetime.now(UTC).strftime('%Y-%m-%d')} | {sku} | {stats.n} | "
        f"{stats.p50_ms:.1f} | {stats.p95_ms:.1f} | {stats.p99_ms:.1f} | "
        f"{stats.total_ms:.1f} | {stats.mean_ms:.1f} |"
    )


def run_benchmark(
    *,
    engagement_id: str,
    bronze_reader: BronzeReader,
    rounds: int,
    scenarios: Iterable[tuple[ControlId, Quarter]] = SCENARIOS,
) -> list[Sample]:
    samples: list[Sample] = []
    for _ in range(rounds):
        for control_id, quarter in scenarios:
            t0 = time.perf_counter()
            extract(
                engagement_id,
                control_id,
                quarter,
                bronze_reader=bronze_reader,
            )
            duration_ms = (time.perf_counter() - t0) * 1000.0
            samples.append(
                Sample(
                    engagement_id=engagement_id,
                    control_id=control_id,
                    quarter=quarter,
                    duration_ms=duration_ms,
                )
            )
    return samples


def _build_conn_factory() -> Any:
    """Wire `databricks.sql.connect` from env vars. Imported lazily so
    unit tests (which never call this) don't need the package installed."""
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
        description="Benchmark Layer 1 extraction across the 8 v2 scenarios."
    )
    parser.add_argument(
        "--engagement",
        default="alpha-pension-fund-2025",
        help="engagement_id to extract (default: alpha-pension-fund-2025)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="number of full sweeps (default: 1; 8 calls per sweep)",
    )
    parser.add_argument(
        "--sku",
        default="serverless-starter",
        help="warehouse SKU label for the summary row (default: serverless-starter)",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="optional path to write per-call timings as JSON",
    )
    args = parser.parse_args(argv)

    factory = _build_conn_factory()
    reader = BronzeReader(factory)

    print(f"Benchmark: engagement={args.engagement}, rounds={args.rounds}, sku={args.sku}")
    samples = run_benchmark(
        engagement_id=args.engagement,
        bronze_reader=reader,
        rounds=args.rounds,
    )
    stats = summarise(samples)

    print()
    print("Per-call timings:")
    for s in samples:
        print(f"  {s.control_id} {s.quarter}: {s.duration_ms:7.1f} ms")
    print()
    print(f"n      = {stats.n}")
    print(f"P50    = {stats.p50_ms:.1f} ms")
    print(f"P95    = {stats.p95_ms:.1f} ms")
    print(f"P99    = {stats.p99_ms:.1f} ms")
    print(f"mean   = {stats.mean_ms:.1f} ms")
    print(f"total  = {stats.total_ms:.1f} ms")

    print()
    print("Markdown row for privateDocs/benchmarks.md:")
    print(format_summary_line(stats, sku=args.sku))

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "engagement_id": args.engagement,
                    "sku": args.sku,
                    "rounds": args.rounds,
                    "samples": [
                        {
                            "control_id": s.control_id,
                            "quarter": s.quarter,
                            "duration_ms": s.duration_ms,
                        }
                        for s in samples
                    ],
                    "stats": {
                        "n": stats.n,
                        "p50_ms": stats.p50_ms,
                        "p95_ms": stats.p95_ms,
                        "p99_ms": stats.p99_ms,
                        "mean_ms": stats.mean_ms,
                        "total_ms": stats.total_ms,
                    },
                },
                f,
                indent=2,
                sort_keys=True,
            )
        print(f"\nWrote per-call JSON to {args.json_out}")

    target_p95_ms = 600.0
    if stats.p95_ms > target_p95_ms:
        print(f"\nFAIL: P95 {stats.p95_ms:.1f} ms exceeds target {target_p95_ms} ms")
        return 1
    print(f"\nPASS: P95 {stats.p95_ms:.1f} ms ≤ target {target_p95_ms} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
