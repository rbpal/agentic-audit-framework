# ai_ops_kit

Shared observability + agent-instrumentation kit for AI workloads.

A standalone Python package providing:

- **OpenTelemetry tracer setup** with OTLP exporter (production) and console fallback (dev)
- **Structured JSON logging** via `structlog` with automatic OTel trace-context injection and sensitive-field redaction
- **Agent-call decorators**: `@traced_tool`, `@traced_agent`, `@traced_llm_call` — emit standardised spans for AI/agent components, sync and async

## Why this exists

This kit is the **platform layer** that Projects 1, 2, and 3 all consume. A span from any project lands in Application Insights with the same schema — meaning one KQL query can investigate behavior across all three. Cross-project observability is the load-bearing reason for the kit, not just code-reuse convenience.

## Consumption model

| Project | How it consumes the kit |
|---|---|
| **Project 1 — agentic-audit-framework** (this repo) | Native — kit lives at `./ai_ops_kit/` in the same repo |
| **Project 2** | Git submodule at `./ai_ops_kit/`, version pinned to a specific commit SHA |
| **Project 3** | Same as Project 2 |

Pinning to commit SHAs (not `main`) means consumer projects never auto-upgrade. To pick up a new kit version: update the submodule pin explicitly, run the consumer's tests, then merge.

## Self-containment guarantee

`ai_ops_kit` must NEVER import from any project-specific code. A CI check (added in `step_09_task_05`) greps the kit for `from agentic_audit` (or analogous Project-2/3 imports) and fails the build if any are found. This is what makes the kit safe to drop into any project without dragging Project 1's dependency closure along.

## Versioning

Semantic versioning. Breaking changes use a deprecation cycle:
- `0.N.0` — adds new behavior, old API still works
- `0.(N+1).0` — removes the deprecated API

Consumers pin to specific SHAs; they don't auto-upgrade across breaking-change boundaries.

## Build & install

```bash
# Standalone (typically run inside this directory):
cd ai_ops_kit
pip install -e ".[dev]"
pytest

# From Project 1 root: kit is already on sys.path; no install needed
poetry run python -c "import ai_ops_kit; print(ai_ops_kit.__version__)"
```

## Layout

```
ai_ops_kit/
├── __init__.py              # version + re-exports
├── pyproject.toml           # standalone package metadata
├── README.md                # this file
├── tracing.py               # step_09_task_02 — OTel tracer + trace_context
├── logging_config.py        # step_09_task_03 — structlog + trace-id injection
├── decorators.py            # step_09_task_04 — @traced_tool, @traced_agent, @traced_llm_call
└── tests/                   # step_09_task_05 — standalone test suite
```

Modules beyond `__init__.py` land in subsequent tasks of Step 9.
