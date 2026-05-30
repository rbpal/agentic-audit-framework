"""Locust load script for the Step-11 AKS load test.

Each simulated user POSTs a random scenario to ``/run`` and validates the
``202`` + body. Business-level failures (a 202 with a wrong/missing body, or
a non-202 status) are counted as failures even when the HTTP layer says 200,
so the report distinguishes transport success from application success.

Locust is a load-gen TOOL, not a project dependency — install it ad-hoc on
the load-gen machine (it pulls gevent, which doesn't build cleanly in this
repo's poetry env):

    pipx install locust            # or: pip install locust  (in a separate venv)

Run against Front Door (or the LB public IP if Front Door is disabled):

    locust -f scripts/load_test.py --host https://<name>.azurefd.net
    # headless example:
    locust -f scripts/load_test.py --host https://<name>.azurefd.net \
        --headless -u 50 -r 5 -t 5m --csv loadtest_50

The 8 scenario ids mirror src/agentic_audit/api/scenarios.py.
"""

from __future__ import annotations

import random

from locust import HttpUser, between, task

# Kept in sync with src/agentic_audit/api/scenarios.py (DC-2/DC-9 × Q1-Q4).
SCENARIO_IDS: list[str] = [
    "dc2_Q1",
    "dc2_Q2",
    "dc2_Q3",
    "dc2_Q4",
    "dc9_Q1",
    "dc9_Q2",
    "dc9_Q3",
    "dc9_Q4",
]


class AuditPipelineUser(HttpUser):
    """Simulates a reviewer submitting workbook scenarios."""

    wait_time = between(1, 5)

    @task
    def submit_scenario(self) -> None:
        scenario_id = random.choice(SCENARIO_IDS)  # noqa: S311 - not security-sensitive
        with self.client.post(
            "/run",
            json={"scenario_id": scenario_id},
            name="POST /run",
            catch_response=True,
        ) as resp:
            if resp.status_code != 202:
                resp.failure(f"expected 202, got {resp.status_code}")
                return
            try:
                body = resp.json()
            except ValueError:
                resp.failure("202 with non-JSON body")
                return
            if body.get("scenario_id") != scenario_id or not body.get("job_id"):
                resp.failure(f"202 with unexpected body: {body!r}")
                return
            resp.success()
