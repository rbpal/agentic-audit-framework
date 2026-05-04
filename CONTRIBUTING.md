# Contributing

This guide gets you from a fresh clone to a green PR. Read once; revisit
the integration-test section before any PR that touches Layer 1 or
Layer 2 code.

## Quick start

```bash
make setup    # Install Poetry deps + pre-commit hooks
make test     # Run unit tests with coverage
make ci       # Run lint + types + tests (what CI runs)
make help     # See every target
```

`make setup` requires Python 3.11 and Poetry already installed.

## Daily workflow

| Command | What it does |
|---|---|
| `make test` | Unit tests + coverage report (gate: 90 %) |
| `make lint` | Ruff lint + format check |
| `make type` | Mypy on `src/` |
| `make ci`   | Everything CI runs (lint + type + test). Run before pushing. |

Pre-commit hooks run on every `git commit` — they enforce ruff format,
trailing whitespace, end-of-file newlines, and a "no hardcoded secrets"
check. If a hook reformats files, the commit fails; just `git add` the
reformatted files and commit again.

## Running integration tests against a live Databricks warehouse

A subset of tests (`@pytest.mark.slow`, in `tests/integration/`) talk
to a real Databricks SQL warehouse. CI **does NOT** run them — they
need workspace credentials and burn warehouse time. **You** are the gate.

### When to run them

**Before merging any PR that touches:**

- `src/agentic_audit/layer1_extract/` — bronze reader, orchestrator,
  silver writer, attribute checks
- `src/agentic_audit/layer2_narrative/silver_reader.py`
- `infra/terraform/modules/databricks_uc/tables_*.tf` (schema changes)
- `scripts/{benchmark_layer1.py,run_layer1.py}`
- Anything that constructs SQL strings or calls Databricks SQL connector

**Why the gate:** unit tests mock `cur.execute()` and only inspect SQL
*strings*, not SQL *semantics*. Two real bugs reached `main` during
`step_05_task_02` because the integration tests that would have caught
them were skipped in CI. See `docs/TECH_DEBT.md` > "@pytest.mark.slow
integration tests" for the full incident write-up. Don't be the third.

### What you need

A Databricks personal access token (PAT) — generate one in the workspace UI:

1. Top-right username → **Settings** → left sidebar **Developer**
2. **Access tokens** section → **Manage** → **Generate new token**
3. **Comment:** anything memorable (e.g., `integration-test-2026-05`)
4. **Lifetime (days):** `1` for one-off runs; longer for ongoing dev
5. **Scope:** click **BI Tools** tab (NOT "Other APIs" — that's the
   common gotcha; "BI Tools" auto-grants the right scope for SQL
   warehouse access)
6. **Generate**, then **copy the token immediately** — Databricks shows
   it once and never again

The token is a remote-shell-equivalent credential. Keep it on your
machine only. Never paste it in chat, GitHub issues, or commits.

### How to run the tests

```bash
# 1. Set the three env vars (interactive; token via silent input)
source scripts/setup_warehouse_env.sh

# 2. Run the integration tests
make integration-test-warehouse
```

The setup script:

- Prompts for `DATABRICKS_HOST` and `DATABRICKS_SQL_WAREHOUSE_ID` with
  dev-environment defaults (override at the prompt for other envs)
- Prompts for `DATABRICKS_TOKEN` via `read -s` — token is invisible as
  you paste, never echoes to terminal, never lands in shell history
- Verifies all three are set; prints HOST + WAREHOUSE values plus
  TOKEN length only (never the token value)

The Make target:

- Pre-flight checks the three env vars; if any are missing, prints a
  clear error pointing back at the setup script and exits without
  running tests
- Runs `pytest -m slow tests/integration/ -v` if env is good

**The `source` is mandatory** — running the script directly
(`./scripts/setup_warehouse_env.sh` or `bash scripts/setup_warehouse_env.sh`)
exports vars in a subshell that exits before the test command runs.
The script refuses to execute non-sourced and prints the right usage.

### Cost / cleanup notes

- Each integration-test run takes ~30–60 seconds against a warm
  serverless warehouse (cold-start adds ~2 minutes to the first call).
- Tests use `engagement_id = "alpha-pension-fund-2025"` and clean up
  their own writes via scoped `DELETE`. They do not pollute silver
  with cross-engagement data.
- The PAT auto-expires per the lifetime you set. Short-lived tokens are
  preferred — generate a fresh one per work session if convenient.

## Commit message style

Look at recent commits for the pattern. Headline format:

```
<type>(<scope>): <short summary> (<step>_<task>)
```

`<type>` is `feat | fix | chore | docs | test`. `<scope>` is the area
touched (`layer1`, `layer2`, `infra`, `models`, etc.). `<step>_<task>`
is the master-plan task identifier when applicable
(e.g. `step_05_task_02b`). Body should explain **why**, not just
**what** — the diff already shows what.

## Where things live

| Path | What |
|---|---|
| `src/agentic_audit/` | All production code. Layered: `models/` (pydantic) → `layer1_extract/` (deterministic) → `layer2_narrative/` (LLM) → `layer3_agents/` (multi-agent, future) |
| `tests/unit/` | Mocked unit tests. Run by `make test` and CI. |
| `tests/integration/` | Live-warehouse tests. `@pytest.mark.slow`, env-gated. Run by `make integration-test-warehouse`. |
| `infra/terraform/` | IaC for the dev environment. Root module is `infra/terraform/`; per-env config under `infra/terraform/envs/dev/`. |
| `scripts/` | Operator tools (driver scripts, env-var helpers). Not part of the runtime pipeline. |
| `docs/` | Long-form repo docs. `TECH_DEBT.md` is the centralized debt register. |
