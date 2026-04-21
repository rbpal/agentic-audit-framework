.PHONY: help setup test lint type ci clean

# Export VIRTUAL_ENV so Poetry consistently uses the in-project .venv.
# Workaround for Poetry 2.3.4's `env use` silently-ignored bug on macOS.
export VIRTUAL_ENV = $(CURDIR)/.venv

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup:  ## Install Poetry deps and pre-commit hooks (first-time bootstrap)
	test -d .venv || python3.11 -m venv .venv
	poetry install --no-interaction
	poetry run pre-commit install
	poetry run pre-commit install --hook-type pre-push

test:  ## Run pytest with coverage
	poetry run pytest --cov=src/agentic_audit --cov-report=term-missing

lint:  ## Run ruff linter + formatter check
	poetry run ruff check .
	poetry run ruff format --check .

type:  ## Run mypy on src/
	poetry run mypy src/

ci: lint type test  ## Run everything CI runs (lint + type + test)

clean:  ## Remove .venv, caches, coverage artifacts
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} +
