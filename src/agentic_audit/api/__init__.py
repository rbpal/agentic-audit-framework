"""HTTP serving layer for the audit pipeline (Step 11 load test).

A thin FastAPI app (`main.py`) accepts scenario submissions and enqueues
them to Azure Service Bus; a separate worker process (`worker.py`) drains
the queue and runs the investigation. The split is the async backpressure
pattern that lets a slow, quota-bound LLM pipeline survive load: the API
returns 202 in milliseconds, the queue absorbs spikes, and KEDA scales
workers to the backlog so the 10K-TPM Azure OpenAI ceiling becomes a
graceful throttle instead of a cascade of 429s.

This module is deployment/serving glue, NOT pipeline logic — the worker's
REAL path reuses the existing ``run_investigation`` call sequence verbatim
(see ``worker.py``). It exists for the transient AKS load test and the
Step 13 demo; it is not on the operator-script path.
"""
