"""Unit tests for the Step-11 load-test API (`agentic_audit.api`).

Runs fully offline: no Service Bus connection string is set, so the app
boots with a NullPublisher, and the worker's MOCK path is exercised with an
injected no-op sleep. The REAL worker path (run_investigation) is covered by
the existing layer3 tests + the live run, not here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic_audit.api import main as api_main
from agentic_audit.api import worker as api_worker
from agentic_audit.api.scenarios import (
    REAL_ELIGIBLE_IDS,
    SCENARIO_IDS,
    SCENARIOS,
)


@pytest.fixture(autouse=True)
def _no_servicebus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee offline mode regardless of the developer's shell env."""
    monkeypatch.delenv("SERVICEBUS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AAF_MOCK_BACKENDS", raising=False)


@pytest.fixture
def client() -> TestClient:
    # `with` triggers lifespan → build_publisher → NullPublisher (no conn str).
    with TestClient(api_main.app) as c:
        yield c


# ── scenarios catalog ────────────────────────────────────────────────


def test_catalog_has_eight_scenarios() -> None:
    assert len(SCENARIO_IDS) == 8
    assert set(SCENARIO_IDS) == {
        "dc2_Q1",
        "dc2_Q2",
        "dc2_Q3",
        "dc2_Q4",
        "dc9_Q1",
        "dc9_Q2",
        "dc9_Q3",
        "dc9_Q4",
    }


def test_prior_quarter_mapping() -> None:
    assert SCENARIOS["dc9_Q1"].prior_quarter is None
    assert SCENARIOS["dc9_Q2"].prior_quarter == "Q1"
    assert SCENARIOS["dc9_Q4"].prior_quarter == "Q3"
    assert SCENARIOS["dc2_Q3"].control_id == "DC-2"


def test_real_eligible_excludes_q1() -> None:
    # Q1 has no prior, so it can't be the current quarter of a comparison.
    assert "dc9_Q1" not in REAL_ELIGIBLE_IDS
    assert "dc2_Q1" not in REAL_ELIGIBLE_IDS
    assert len(REAL_ELIGIBLE_IDS) == 6


# ── API endpoints ────────────────────────────────────────────────────


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_scenarios_endpoint(client: TestClient) -> None:
    resp = client.get("/scenarios")
    assert resp.status_code == 200
    assert sorted(resp.json()["scenarios"]) == sorted(SCENARIO_IDS)


def test_run_accepts_valid_scenario_and_enqueues(client: TestClient) -> None:
    resp = client.post("/run", json={"scenario_id": "dc9_Q2"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["scenario_id"] == "dc9_Q2"
    assert body["status"] == "queued"
    assert len(body["job_id"]) == 32  # uuid4 hex
    # NullPublisher recorded the enqueue
    published = client.app.state.publisher.published
    assert published == [(body["job_id"], "dc9_Q2")]


def test_run_rejects_unknown_scenario(client: TestClient) -> None:
    resp = client.post("/run", json={"scenario_id": "bogus_Q9"})
    assert resp.status_code == 422
    assert "unknown scenario_id" in resp.json()["detail"]


def test_build_publisher_defaults_to_null_when_no_conn_string() -> None:
    assert isinstance(api_main.build_publisher(), api_main.NullPublisher)


# ── worker MOCK path ─────────────────────────────────────────────────


def test_process_scenario_mock_returns_synthetic_without_sleeping() -> None:
    slept: list[float] = []
    result = api_worker.process_scenario("dc9_Q3", pipeline=None, sleep=slept.append)
    assert result.mode == "mock"
    assert result.recommendation == "MOCK"
    assert result.status == "concluded"
    assert len(slept) == 1  # asked to sleep exactly once
    assert slept[0] >= 0  # a non-negative latency was drawn


def test_process_scenario_unknown_id_raises() -> None:
    with pytest.raises(ValueError, match="unknown scenario_id"):
        api_worker.process_scenario("nope", pipeline=None, sleep=lambda _: None)


def test_mock_latency_bounds_normalise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_LATENCY_MIN_S", "8")
    monkeypatch.setenv("MOCK_LATENCY_MAX_S", "4")  # inverted on purpose
    lo, hi = api_worker._mock_latency_bounds()
    assert (lo, hi) == (4.0, 8.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("no", False)],
)
def test_mock_enabled_parsing(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("AAF_MOCK_BACKENDS", value)
    assert api_worker._mock_enabled() is expected
