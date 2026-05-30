"""FastAPI app — the load-test ingress.

``POST /run {scenario_id}`` validates the id against the catalog, publishes
a job message to Azure Service Bus, and returns ``202`` immediately. The
heavy work happens out-of-band in the worker (``worker.py``); the API never
blocks on an investigation. ``GET /healthz`` backs the K8s liveness/readiness
probes; ``GET /scenarios`` lists the catalog (handy for the Locust script).

The publisher is an injectable abstraction so unit tests run with no Azure:
- ``ServiceBusPublisher`` — real, built from ``SERVICEBUS_CONNECTION_STRING``.
- ``NullPublisher`` — records publishes in-memory; used when no connection
  string is set (local dev / tests) so ``/healthz`` and ``/run`` work offline.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from agentic_audit.api.scenarios import SCENARIO_IDS, SCENARIOS

logger = logging.getLogger(__name__)

_QUEUE_NAME_DEFAULT = "investigations"


class RunRequest(BaseModel):
    scenario_id: str


class RunAccepted(BaseModel):
    job_id: str
    scenario_id: str
    status: str = "queued"


class Publisher(Protocol):
    """Enqueues a job. Implementations must be thread-safe enough to be
    called via ``run_in_threadpool`` from concurrent requests."""

    def publish(self, *, job_id: str, scenario_id: str) -> None: ...

    def close(self) -> None: ...


class NullPublisher:
    """No-Azure publisher — records publishes in memory. Used offline."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish(self, *, job_id: str, scenario_id: str) -> None:
        self.published.append((job_id, scenario_id))
        logger.info("NullPublisher: would enqueue job=%s scenario=%s", job_id, scenario_id)

    def close(self) -> None:  # noqa: D401 - trivial
        return None


class ServiceBusPublisher:
    """Real Service Bus publisher. One client for the app lifetime; a fresh
    sender per publish (cheap, and avoids cross-thread sender sharing)."""

    def __init__(self, connection_string: str, queue_name: str) -> None:
        from azure.servicebus import ServiceBusClient

        self._client = ServiceBusClient.from_connection_string(connection_string)
        self._queue_name = queue_name

    def publish(self, *, job_id: str, scenario_id: str) -> None:
        from azure.servicebus import ServiceBusMessage

        payload = json.dumps({"job_id": job_id, "scenario_id": scenario_id})
        with self._client.get_queue_sender(self._queue_name) as sender:
            sender.send_messages(ServiceBusMessage(payload))

    def close(self) -> None:
        self._client.close()


def build_publisher() -> Publisher:
    """Pick the publisher from the environment. No connection string →
    NullPublisher so the app boots and serves offline."""
    connection_string = os.getenv("SERVICEBUS_CONNECTION_STRING")
    if not connection_string:
        logger.warning(
            "SERVICEBUS_CONNECTION_STRING unset — using NullPublisher "
            "(jobs are not enqueued). Set it to publish to a real queue."
        )
        return NullPublisher()
    queue_name = os.getenv("SERVICEBUS_QUEUE_NAME", _QUEUE_NAME_DEFAULT)
    return ServiceBusPublisher(connection_string, queue_name)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.publisher = build_publisher()
    try:
        yield
    finally:
        app.state.publisher.close()


app = FastAPI(title="agentic-audit load-test API", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/scenarios")
async def scenarios() -> dict[str, list[str]]:
    return {"scenarios": SCENARIO_IDS}


@app.post("/run", status_code=202, response_model=RunAccepted)
async def run(req: RunRequest) -> RunAccepted:
    if req.scenario_id not in SCENARIOS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown scenario_id {req.scenario_id!r}; valid ids: {SCENARIO_IDS}",
        )
    job_id = uuid.uuid4().hex
    # Enqueue off the event loop — the Service Bus client is sync.
    await run_in_threadpool(app.state.publisher.publish, job_id=job_id, scenario_id=req.scenario_id)
    return RunAccepted(job_id=job_id, scenario_id=req.scenario_id)
