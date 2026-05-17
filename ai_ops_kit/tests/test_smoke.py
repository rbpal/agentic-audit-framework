"""Standalone smoke tests for ai_ops_kit.

Runnable in isolation (without Project 1) via::

    cd ai_ops_kit
    pip install -e ".[dev]"
    pytest

These tests verify the kit's public API works end-to-end without depending
on any project-specific code. They are the **cross-project contract** —
what Projects 2 and 3 will rely on as the kit's stable surface.

Deep behavior tests (processor chains, edge cases, error paths) live in
Project 1's ``tests/unit/observability/``. This file deliberately stays
smoke-only — if a consumer's install works and these pass, the kit's
contract is intact.

NO ``__init__.py`` in this directory by design — pytest auto-discovers via
``[tool.pytest.ini_options]`` ``testpaths = ["tests"]`` in the kit's
pyproject.toml. Adding ``__init__.py`` would make tests/ a sub-package
that setuptools would install into site-packages along with the kit.
"""

from __future__ import annotations

from ai_ops_kit import (
    __version__,
    configure_logging,
    get_tracer,
    init_tracer,
    trace_context,
    traced_agent,
    traced_llm_call,
    traced_tool,
)


def test_version_is_set() -> None:
    assert __version__ == "0.1.0"


def test_all_public_symbols_are_importable() -> None:
    """If the imports at the top of this file succeed, the kit's surface is intact.

    This test exists as an explicit assertion of that contract — a failure
    here means a consumer's ``from ai_ops_kit import ...`` would break.
    """
    for symbol in (
        __version__,
        configure_logging,
        get_tracer,
        init_tracer,
        trace_context,
        traced_agent,
        traced_llm_call,
        traced_tool,
    ):
        assert symbol is not None


def test_configure_logging_does_not_raise() -> None:
    configure_logging()


def test_init_tracer_returns_tracer() -> None:
    assert init_tracer("smoke_service") is not None


def test_get_tracer_returns_tracer() -> None:
    assert get_tracer("smoke.module") is not None


def test_trace_context_yields_a_span() -> None:
    with trace_context("smoke_span", scenario_id="test", run_id="abc") as span:
        assert span is not None


def test_traced_tool_decorates_sync_function() -> None:
    @traced_tool()
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_traced_tool_with_explicit_name_works() -> None:
    @traced_tool(tool_name="explicit_name")
    def fn() -> str:
        return "ok"

    assert fn() == "ok"


def test_traced_agent_decorates_function() -> None:
    @traced_agent()
    def example_agent() -> dict:
        return {"iterations": 1, "total_tokens": 100}

    assert example_agent() == {"iterations": 1, "total_tokens": 100}


def test_traced_llm_call_decorates_function() -> None:
    @traced_llm_call(model="gpt-4o")
    def example_llm() -> dict:
        return {"prompt_tokens": 10, "completion_tokens": 5}

    assert example_llm() == {"prompt_tokens": 10, "completion_tokens": 5}


def test_traced_tool_exception_propagates() -> None:
    @traced_tool()
    def doomed() -> None:
        raise RuntimeError("by design")

    try:
        doomed()
    except RuntimeError as exc:
        assert str(exc) == "by design"
    else:
        raise AssertionError("expected RuntimeError")
