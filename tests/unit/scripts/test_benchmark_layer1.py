"""Unit tests for `scripts/benchmark_layer1.py` math.

The live-warehouse runner is not exercised in CI (no warehouse creds);
these tests verify the percentile + summarise + format-summary-line
logic that produces the numbers the operator pastes into
`privateDocs/benchmarks.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `scripts/` isn't a package on PYTHONPATH by default; add it explicitly.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

from benchmark_layer1 import (  # type: ignore[import-not-found]  # noqa: E402
    Sample,
    Stats,
    format_summary_line,
    percentile,
    summarise,
)

# ---------- percentile --------------------------------------------------


def test_percentile_p50_of_odd_length() -> None:
    """50th percentile of 1..9 → middle element."""
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0], 0.5) == 5.0


def test_percentile_p95_of_uniform() -> None:
    """95th percentile of 100 evenly-spaced values picks index 95."""
    sorted_vals = [float(i) for i in range(100)]
    assert percentile(sorted_vals, 0.95) == 95.0


def test_percentile_p99_clamps_at_max_index() -> None:
    """99th percentile of 8 values → index min(7, int(0.99*8)=7)."""
    sorted_vals = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
    assert percentile(sorted_vals, 0.99) == 80.0


def test_percentile_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty list"):
        percentile([], 0.5)


def test_percentile_p100_returns_max() -> None:
    sorted_vals = [10.0, 20.0, 30.0]
    assert percentile(sorted_vals, 1.0) == 30.0


def test_percentile_p0_returns_min() -> None:
    sorted_vals = [10.0, 20.0, 30.0]
    assert percentile(sorted_vals, 0.0) == 10.0


# ---------- summarise --------------------------------------------------


def _sample(control: str, quarter: str, ms: float) -> Sample:
    return Sample(
        engagement_id="alpha-pension-fund-2025",
        control_id=control,
        quarter=quarter,
        duration_ms=ms,
    )


def test_summarise_eight_scenario_sweep_under_target() -> None:
    samples = [
        _sample("DC-2", "Q1", 120.0),
        _sample("DC-2", "Q2", 130.0),
        _sample("DC-2", "Q3", 140.0),
        _sample("DC-2", "Q4", 150.0),
        _sample("DC-9", "Q1", 200.0),
        _sample("DC-9", "Q2", 210.0),
        _sample("DC-9", "Q3", 220.0),
        _sample("DC-9", "Q4", 230.0),
    ]
    stats = summarise(samples)
    assert stats.n == 8
    assert stats.p50_ms == 175.0
    assert stats.p95_ms == 230.0  # int(0.95 * 8) = 7 → last index
    assert stats.p99_ms == 230.0
    assert stats.total_ms == sum(s.duration_ms for s in samples)
    assert stats.mean_ms == pytest.approx(175.0)


def test_summarise_one_outlier_pulls_p95_above_target() -> None:
    """One slow call should be visible in P95 even if the rest are fast."""
    samples = [_sample("DC-2", "Q1", 100.0)] * 19 + [_sample("DC-9", "Q4", 800.0)]
    stats = summarise(samples)
    assert stats.n == 20
    assert stats.p50_ms == 100.0
    assert stats.p95_ms == 800.0  # int(0.95 * 20) = 19 → last index
    assert stats.p99_ms == 800.0


def test_summarise_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty sample list"):
        summarise([])


# ---------- format_summary_line ----------------------------------------


def test_format_summary_line_renders_markdown_row() -> None:
    stats = Stats(n=8, p50_ms=120.5, p95_ms=230.4, p99_ms=240.1, total_ms=1500.7, mean_ms=187.6)
    line = format_summary_line(stats, sku="serverless-starter")
    assert line.startswith("|")
    assert line.endswith("|")
    # Date is dynamic; check the rest of the columns
    assert "| serverless-starter | 8 |" in line
    assert "120.5 |" in line
    assert "230.4 |" in line
    assert "240.1 |" in line
    assert "1500.7 |" in line
    assert "187.6 |" in line


def test_format_summary_line_passes_sku_through() -> None:
    stats = Stats(n=8, p50_ms=1.0, p95_ms=2.0, p99_ms=3.0, total_ms=4.0, mean_ms=5.0)
    assert "| pro-medium |" in format_summary_line(stats, sku="pro-medium")
