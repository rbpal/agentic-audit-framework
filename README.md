# agentic-audit-framework

Multi-agent SOX control testing on Azure + Databricks. LangGraph supervisor orchestrating deterministic extraction, grounded narrative generation, and agentic exception investigation. Production engineering: MLflow, OpenTelemetry, Unity Catalog, autoscaling, DR plan, Terraform IaC.

## Status

| | |
|---|---|
| CI | [![CI](https://github.com/rbpal/agentic-audit-framework/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/rbpal/agentic-audit-framework/actions/workflows/ci.yml) |
| Python | 3.11 |
| Package manager | Poetry 2.3+ |
| License | MIT |

## Quickstart

```bash
git clone https://github.com/rbpal/agentic-audit-framework.git
cd agentic-audit-framework
cp .env.example .env    # then fill in Azure OpenAI credentials
make setup              # installs deps + pre-commit hooks
make ci                 # lint + type + test — must exit 0
```

## Make targets

```
help      Show this help
setup     Install Poetry deps and pre-commit hooks (first-time bootstrap)
test      Run pytest with coverage
lint      Run ruff linter + formatter check
type      Run mypy on src/
ci        Run everything CI runs (lint + type + test)
clean     Remove .venv, caches, coverage artifacts
```

## Roadmap

Step 0 — **Repo scaffold, Poetry, pre-commit, CI basics** — done.

Steps 1–14 (planned):

1. Layer 1 — Excel control extraction
2. Delta materialization on Databricks
3. Unity Catalog lineage + governance
4. Layer 2 — grounded narrative generation with fact checking
5. Layer 3 — agentic exception investigation
6. Evaluation harness with gold scenarios
7. Streamlit demo
8. Azure infra (Terraform IaC)
9. OpenTelemetry tracing
10. MLflow experiment tracking
11. Deployment to Azure Container Apps
12. Disaster recovery plan
13. Streamlit demo polish
14. Docs and ship

## Architecture

Three layers, supervised by a LangGraph orchestrator:

- **Layer 1 — Extraction**: deterministic parsing of SOX control Excel workbooks into typed evidence records.
- **Layer 2 — Narrative**: grounded natural-language control summaries with per-claim fact checking against source evidence.
- **Layer 3 — Investigation**: agentic exception handling — the agent identifies weak or missing controls, generates follow-up questions, and drafts remediation notes.

## License

MIT. See [LICENSE](LICENSE).
