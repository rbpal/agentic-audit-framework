# Container image for the Step-11 load-test serving layer.
#
# ONE image, TWO roles (the worker overrides the command):
#   API     (default CMD): uvicorn agentic_audit.api.main:app
#   Worker  (cmd override): python -m agentic_audit.api.worker
#
# Deps are installed from the locked pyproject (main group only — no dev,
# no test tooling). The package itself isn't pip-installed; PYTHONPATH=/app/src
# makes `agentic_audit` importable, which keeps the layer simple.
#
# Note: the main dependency set is heavy (langgraph/langchain/mlflow/pandas),
# so this image is large. Fine for a transient load test — nodes cache it
# after first pull, so only initial deploy + cluster-autoscaler new-node
# scale-up re-pull. A slimmer image (optional-dep groups / multi-stage) is a
# deferred optimization, not needed for the demo.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install --no-cache-dir "poetry>=2.0,<3.0"

# Dependency layer — cached unless pyproject/lock change. README is copied
# because pyproject references it (readme = "README.md").
COPY pyproject.toml poetry.lock README.md ./
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-root

# App code — changes here don't bust the dependency layer above.
COPY src/ ./src/
ENV PYTHONPATH=/app/src

EXPOSE 8000

# Default role = the API. The worker Deployment overrides this with:
#   command: ["python", "-m", "agentic_audit.api.worker"]
CMD ["uvicorn", "agentic_audit.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
