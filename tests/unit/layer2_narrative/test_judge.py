"""Unit tests for ``agentic_audit.layer2_narrative.judge``.

Mocks the Azure OpenAI client entirely — no live calls, no
``azure-identity`` dep at test time. Live round-trip exercised by
``tests/integration/test_layer2_judge_e2e.py`` (planned, env-gated).

Coverage matrix:

- Construction: explicit client wins over factory; ``from_env`` reads
  endpoint env var; pinned prompt + deployment.
- ``evaluate`` happy paths: pass / fail / uncertain verdicts round-trip
  through JSON-mode parsing into a valid ``JudgeResponse``.
- Retry-and-fallback: malformed-JSON-then-recover, malformed-JSON-x2
  → uncertain fallback, ``ValidationError`` x2 → uncertain fallback,
  empty-content x2 → uncertain fallback.
- Recorder invocation: when wired, every billed call records token
  usage, including the ones that get retried.
- Prompt template usage: the rendered prompt carries every input
  placeholder substituted, never reaches the LLM with raw ``${...}``
  markers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agentic_audit.layer2_narrative.judge import Judge
from agentic_audit.models.evidence import (
    AttributeCheck,
    ExtractedEvidence,
    SignOff,
)
from agentic_audit.models.judge import JudgeResponse
from agentic_audit.models.narrative import AttributeNarrative
from agentic_audit.models.telemetry import UsageRecorder

UTC_TS = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


# ---------- fixtures ---------------------------------------------------


def _make_evidence(
    *,
    control_id: str = "DC-9",
    quarter: str = "Q1",
) -> ExtractedEvidence:
    attribute_ids = ["A", "B", "C", "D", "E", "F"] if control_id == "DC-9" else ["A", "B", "C", "D"]
    attrs = [
        AttributeCheck(
            control_id=control_id,  # type: ignore[arg-type]
            attribute_id=a,  # type: ignore[arg-type]
            status="pass",
            evidence_cell_refs=[f"{control_id.replace('-', '')}_WP!{a}1"],
            extracted_value={"sample": f"val-{a}"},
            notes=f"check {a}",
        )
        for a in attribute_ids
    ]
    return ExtractedEvidence(
        engagement_id="alpha-pension-fund-2025",
        control_id=control_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        run_id="01J0F7M5XQXM2QYAY8X8X8X8X8",
        extraction_timestamp=UTC_TS,
        preparer=SignOff(initials="AB", role="preparer", date=UTC_TS),
        reviewer=SignOff(initials="CD", role="reviewer", date=UTC_TS),
        attributes=attrs,
        source_bronze_file_hash="a" * 64,
        source_path=f"/bronze/dc{control_id.split('-')[1]}_{quarter}_ref.xlsx",
    )


def _make_narrative(
    *,
    control_id: str = "DC-9",
    attribute_id: str = "A",
    quarter: str = "Q1",
    narrative_text: str = "Preparer AB signed DC-9.A on 2026-05-10.",
    cited_fields: list[str] | None = None,
) -> AttributeNarrative:
    if cited_fields is None:
        cited_fields = ["DC9_WP!A1"]
    return AttributeNarrative(
        engagement_id="alpha-pension-fund-2025",
        control_id=control_id,  # type: ignore[arg-type]
        attribute_id=attribute_id,  # type: ignore[arg-type]
        quarter=quarter,  # type: ignore[arg-type]
        source_evidence_id=f"alpha|{control_id}|{attribute_id}|{quarter}",
        narrative_text=narrative_text,
        cited_fields=cited_fields,
        word_count=len(narrative_text.split()),
        prompt_version="v1.0",
        model_deployment="gpt-4o",
        generation_run_id="GEN-RUN-001",
        generated_at=UTC_TS,
    )


def _build_chat_completion_response(
    payload_json: str,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    finish_reason: str = "stop",
) -> MagicMock:
    """Mock an OpenAI ChatCompletion that yields the given JSON body.
    Token usage is included only when explicitly supplied — keeps the
    no-recorder tests free of MagicMock-as-int gotchas."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = payload_json
    response.choices[0].finish_reason = finish_reason
    if prompt_tokens is not None and completion_tokens is not None:
        response.usage = MagicMock()
        response.usage.prompt_tokens = prompt_tokens
        response.usage.completion_tokens = completion_tokens
    else:
        response.usage = None
    return response


def _make_judge(client: MagicMock, recorder: UsageRecorder | None = None) -> Judge:
    return Judge(
        endpoint="https://aoai-aaf-rbpal-dev.openai.azure.com/",
        deployment="gpt-4o",
        prompt_version="judge_v1.0",
        client=client,
        usage_recorder=recorder,
    )


# ---------- construction -----------------------------------------------


def test_judge_init_with_explicit_client_does_not_build_one() -> None:
    fake_client = MagicMock()
    j = Judge(
        endpoint="https://aoai-aaf-rbpal-dev.openai.azure.com/",
        deployment="gpt-4o",
        prompt_version="judge_v1.0",
        client=fake_client,
    )
    assert j.endpoint == "https://aoai-aaf-rbpal-dev.openai.azure.com/"
    assert j.deployment == "gpt-4o"
    assert j.prompt_version == "judge_v1.0"
    assert j._client is fake_client


def test_judge_default_deployment_and_prompt_version() -> None:
    """v1 production posture: gpt-4o for vendor parity with the
    generator; judge_v1.0 for the prompt rev that ships with task_03."""
    j = Judge(endpoint="x", client=MagicMock())
    assert j.deployment == "gpt-4o"
    assert j.prompt_version == "judge_v1.0"


def test_judge_from_env_reads_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://test-endpoint.example.com/")
    monkeypatch.setattr(
        "agentic_audit.layer2_narrative.judge._build_azure_openai_client",
        lambda **kwargs: MagicMock(name="FakeAzureOpenAI"),
    )
    j = Judge.from_env()
    assert j.endpoint == "https://test-endpoint.example.com/"


def test_judge_from_env_raises_when_endpoint_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        Judge.from_env()


# ---------- evaluate() — happy paths -----------------------------------


def test_evaluate_returns_pass_verdict() -> None:
    payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.92,
            "reasoning": "Reviewer signoff present in evidence; narrative correctly conveys it.",
            "cited_evidence_fields": ["reviewer.initials", "reviewer.date"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _build_chat_completion_response(payload)
    j = _make_judge(fake_client)

    result = j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="DC-9.A — preparer signoff",
    )

    assert isinstance(result, JudgeResponse)
    assert result.verdict == "pass"
    assert result.confidence == pytest.approx(0.92)
    assert result.cited_evidence_fields == ["reviewer.initials", "reviewer.date"]


def test_evaluate_returns_fail_verdict() -> None:
    payload = json.dumps(
        {
            "verdict": "fail",
            "confidence": 0.85,
            "reasoning": "Narrative claims management reviewed; evidence shows reviewer cell blank.",
            "cited_evidence_fields": ["reviewer.initials"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _build_chat_completion_response(payload)
    j = _make_judge(fake_client)

    result = j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="fail",
        attribute_definition="DC-9.A — preparer signoff",
    )

    assert result.verdict == "fail"


def test_evaluate_returns_uncertain_verdict_with_empty_cited_fields() -> None:
    """Uncertain is the legitimate empty-citation case — Decision Rule
    1 exempts it. Make sure the judge can return uncertain end-to-end."""
    payload = json.dumps(
        {
            "verdict": "uncertain",
            "confidence": 0.4,
            "reasoning": "Evidence does not unambiguously support pass or fail.",
            "cited_evidence_fields": [],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _build_chat_completion_response(payload)
    j = _make_judge(fake_client)

    result = j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="DC-9.A — preparer signoff",
    )

    assert result.verdict == "uncertain"
    assert result.cited_evidence_fields == []


# ---------- evaluate() — LLM call shape --------------------------------


def test_evaluate_calls_llm_with_json_mode_temp_zero_max_tokens_500() -> None:
    payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.9,
            "reasoning": "ok",
            "cited_evidence_fields": ["x"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _build_chat_completion_response(payload)
    j = _make_judge(fake_client)

    j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )

    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["temperature"] == 0
    assert kwargs["max_tokens"] == 500
    assert kwargs["response_format"] == {"type": "json_object"}


def test_evaluate_prompt_substitutes_every_placeholder() -> None:
    """The rendered prompt sent to the LLM must have every ${...}
    placeholder substituted. If a placeholder leaks through, the LLM
    sees raw template syntax — catch the prompt drift here."""
    payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.9,
            "reasoning": "ok",
            "cited_evidence_fields": ["x"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _build_chat_completion_response(payload)
    j = _make_judge(fake_client)

    j.evaluate(
        _make_narrative(narrative_text="A grounded narrative about Q1."),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="DC-9.A — preparer signoff",
    )

    prompt = fake_client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    # No leftover placeholders
    for placeholder in (
        "${narrative_text}",
        "${cited_fields}",
        "${evidence_json}",
        "${attribute_definition}",
        "${gold_expected_verdict}",
    ):
        assert placeholder not in prompt
    # Substituted values present
    assert "A grounded narrative about Q1." in prompt
    assert "DC-9.A — preparer signoff" in prompt
    # Evidence envelope reused from the generator's static helper
    assert "alpha-pension-fund-2025" in prompt
    assert "attribute_check" in prompt


# ---------- evaluate() — retry / fallback ------------------------------


def test_evaluate_recovers_when_json_parses_on_retry() -> None:
    """Malformed JSON on first attempt, valid on second → second
    response wins, no fallback fires."""
    valid_payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.9,
            "reasoning": "ok",
            "cited_evidence_fields": ["x"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        _build_chat_completion_response("not valid json {{{"),
        _build_chat_completion_response(valid_payload),
    ]
    j = _make_judge(fake_client)

    result = j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )

    assert result.verdict == "pass"
    assert fake_client.chat.completions.create.call_count == 2


def test_evaluate_falls_back_to_uncertain_after_two_json_decode_errors() -> None:
    """The harness must not block the sweep — two parse failures
    yield a deterministic uncertain row, not an exception."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        _build_chat_completion_response("not valid {{{"),
        _build_chat_completion_response("still not valid }}}"),
    ]
    j = _make_judge(fake_client)

    result = j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )

    assert result.verdict == "uncertain"
    assert result.confidence == 0.0
    assert result.cited_evidence_fields == []
    # Reasoning carries the diagnostic for grepping eval_outcomes
    assert "judge failure x2" in result.reasoning
    assert "JSON parse failure" in result.reasoning


def test_evaluate_falls_back_to_uncertain_after_two_validation_errors() -> None:
    """JSON parses but JudgeResponse validator rejects (e.g. pass
    verdict with empty cited_evidence_fields). Same fallback shape."""
    bad_payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.9,
            "reasoning": "ok",
            "cited_evidence_fields": [],  # violates Decision Rule 1
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        _build_chat_completion_response(bad_payload),
        _build_chat_completion_response(bad_payload),
    ]
    j = _make_judge(fake_client)

    result = j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )

    assert result.verdict == "uncertain"
    assert "validation failure x2" in result.reasoning


def test_evaluate_falls_back_to_uncertain_after_two_empty_responses() -> None:
    """Empty content (e.g. finish_reason='length') counts as a parse
    failure. Two in a row → fallback."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        _build_chat_completion_response("", finish_reason="length"),
        _build_chat_completion_response("", finish_reason="length"),
    ]
    # Override content to None for the empty-response simulation
    fake_client.chat.completions.create.side_effect = [
        _make_empty_response("length"),
        _make_empty_response("length"),
    ]
    j = _make_judge(fake_client)

    result = j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )

    assert result.verdict == "uncertain"
    assert "empty content x2" in result.reasoning


def _make_empty_response(finish_reason: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = None
    response.choices[0].finish_reason = finish_reason
    response.usage = None
    return response


def test_evaluate_logs_warning_on_fallback(caplog) -> None:
    """The fallback must be observable in logs — operators grep WARN
    to find sweeps where the judge struggled."""
    import logging

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        _build_chat_completion_response("not valid {{{"),
        _build_chat_completion_response("still not valid }}}"),
    ]
    j = _make_judge(fake_client)

    with caplog.at_level(logging.WARNING):
        j.evaluate(
            _make_narrative(),
            _make_evidence(),
            gold_expected_verdict="pass",
            attribute_definition="x",
        )

    warn_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("retrying" in m for m in warn_messages)


# ---------- evaluate() — usage recorder --------------------------------


def test_evaluate_records_usage_when_recorder_wired() -> None:
    payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.9,
            "reasoning": "ok",
            "cited_evidence_fields": ["x"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _build_chat_completion_response(
        payload, prompt_tokens=120, completion_tokens=40
    )
    recorder = UsageRecorder()
    j = _make_judge(fake_client, recorder=recorder)

    j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )

    assert recorder.n_calls == 1
    assert recorder.prompt_tokens == 120
    assert recorder.completion_tokens == 40


def test_evaluate_records_usage_for_both_attempts_on_retry() -> None:
    """Retry was a billed call too — recorder accumulates both."""
    valid_payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.9,
            "reasoning": "ok",
            "cited_evidence_fields": ["x"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [
        _build_chat_completion_response("not valid {{{", prompt_tokens=100, completion_tokens=30),
        _build_chat_completion_response(valid_payload, prompt_tokens=110, completion_tokens=50),
    ]
    recorder = UsageRecorder()
    j = _make_judge(fake_client, recorder=recorder)

    j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )

    assert recorder.n_calls == 2
    assert recorder.prompt_tokens == 210
    assert recorder.completion_tokens == 80


def test_evaluate_no_recorder_does_not_break() -> None:
    """Default no-recorder path — backwards-compat with callers that
    don't supply one."""
    payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.9,
            "reasoning": "ok",
            "cited_evidence_fields": ["x"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _build_chat_completion_response(payload)
    j = _make_judge(fake_client)  # no recorder

    result = j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )
    assert result.verdict == "pass"


def test_evaluate_handles_response_without_usage_block_gracefully() -> None:
    """Defensive: if a future SDK drops the usage block, the recorder
    skips that call rather than crashing the whole sweep."""
    payload = json.dumps(
        {
            "verdict": "pass",
            "confidence": 0.9,
            "reasoning": "ok",
            "cited_evidence_fields": ["x"],
        }
    )
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _build_chat_completion_response(payload)
    # _build_chat_completion_response sets usage=None when token
    # counts are not supplied
    recorder = UsageRecorder()
    j = _make_judge(fake_client, recorder=recorder)

    j.evaluate(
        _make_narrative(),
        _make_evidence(),
        gold_expected_verdict="pass",
        attribute_definition="x",
    )

    # No record because the response had no usage block
    assert recorder.n_calls == 0
