"""LLM-as-judge for the eval harness (Step 6 task_03).

The shape:

- ``Judge.evaluate(narrative, evidence, gold_expected_verdict,
  attribute_definition) -> JudgeResponse`` is the production entry
  point. It renders ``judge_v1_0.txt`` against the inputs, calls Azure
  OpenAI in JSON mode, parses + validates the response, and returns a
  ``JudgeResponse`` that the sweep driver writes to
  ``audit_dev.gold.eval_outcomes``.
- On any LLM failure (empty content, malformed JSON, validation
  failure on ``JudgeResponse``), the judge **does not raise**. It
  retries once with the same prompt; on a second failure, it returns
  ``JudgeResponse(verdict="uncertain", confidence=0.0, reasoning="judge
  failure x2: <reason>", cited_evidence_fields=[])`` and logs a WARN.
  Rationale: the eval harness is observability, not a control-flow
  gate — judge failure must never block the sweep.

Design choices baked in (per ``privateDocs/step_06_eval_harness.md``
task_03):

- **Same client construction as ``NarrativeGenerator``** — managed
  identity, lazy import, ``DefaultAzureCredential`` chain. Judge runs
  in the same Azure OpenAI tenant as the generator; auth is shared.
- **Pinned ``prompt_version`` + ``model_deployment`` at init time** —
  every judge call carries the exact prompt + model; the eval-outcomes
  table groups by these for prompt iteration A/B comparisons.
- **Optional ``usage_recorder``** — same shape as
  ``NarrativeGenerator``. Judge sweeps record their own
  ``agent_run_id`` (distinct from generator sweeps) so cost dashboards
  can split judge-cost from generator-cost cleanly.
- **Reuses ``NarrativeGenerator._build_evidence_json``** for the
  evidence payload. The judge needs to see exactly what the generator
  saw — same per-attribute slice, not the full ``ExtractedEvidence``.
"""

from __future__ import annotations

import json
import logging
import os
from string import Template
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agentic_audit.layer2_narrative.generator import (
    AZURE_OPENAI_API_VERSION,
    NarrativeGenerator,
    _build_azure_openai_client,
)
from agentic_audit.layer2_narrative.prompt_loader import load_prompt
from agentic_audit.models.evidence import ExtractedEvidence
from agentic_audit.models.judge import JudgeResponse
from agentic_audit.models.narrative import AttributeNarrative
from agentic_audit.models.telemetry import CallUsage, UsageRecorder
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from openai import AzureOpenAI

logger = logging.getLogger(__name__)


# Match the generator's max_tokens — the JSON envelope (verdict +
# confidence + reasoning + cited_evidence_fields) is smaller than the
# narrative envelope, but reasoning text can be a paragraph and
# cited_evidence_fields a multi-element array. 500 gives comfortable
# headroom and avoids a "judge truncation" failure mode parallel to
# the one task_07 hit on narrative generation.
_MAX_TOKENS = 500


class Judge:
    """LLM-as-judge for narrative eval (task_03).

    Holds the Azure OpenAI client + the deployment / prompt-version
    pinning. Exposes one production entry point — ``evaluate(...)`` —
    and a deterministic uncertain-fallback that fires when the LLM
    fails to produce a parseable, valid response twice in a row.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str = "gpt-4o",
        prompt_version: str = "judge_v1.0",
        client: AzureOpenAI | None = None,
        api_version: str = AZURE_OPENAI_API_VERSION,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        """Build a judge pinned to a specific deployment + prompt version.

        Defaults match the v1 production posture: ``gpt-4o`` for
        same-vendor parity with the generator (different-vendor judge
        is a deferred follow-up); ``judge_v1.0`` for the prompt rev
        that ships with this task.

        ``client`` is dependency-injected for tests; in production
        wiring it is None and the constructor builds one via
        ``_build_azure_openai_client`` (same factory the generator
        uses). ``usage_recorder`` is optional — when supplied, every
        billed LLM call is recorded for the sweep's cost-telemetry row.
        """
        self._endpoint = endpoint
        self._deployment = deployment
        self._prompt_version = prompt_version
        self._client = (
            client
            if client is not None
            else _build_azure_openai_client(endpoint=endpoint, api_version=api_version)
        )
        self._usage_recorder = usage_recorder

    @classmethod
    def from_env(
        cls,
        *,
        deployment: str = "gpt-4o",
        prompt_version: str = "judge_v1.0",
        endpoint_env_var: str = "AZURE_OPENAI_ENDPOINT",
        usage_recorder: UsageRecorder | None = None,
    ) -> Judge:
        """Build from ``AZURE_OPENAI_ENDPOINT`` env var. Same env-var
        contract as ``NarrativeGenerator.from_env``."""
        endpoint = os.environ.get(endpoint_env_var)
        if not endpoint:
            raise RuntimeError(
                f"environment variable {endpoint_env_var!r} is not set; "
                "either export it (e.g. "
                "https://aoai-aaf-rbpal-dev.openai.azure.com/) or pass "
                "endpoint explicitly to Judge(...)."
            )
        return cls(
            endpoint=endpoint,
            deployment=deployment,
            prompt_version=prompt_version,
            usage_recorder=usage_recorder,
        )

    @property
    def deployment(self) -> str:
        return self._deployment

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @traced_function("layer2.judge.evaluate")
    def evaluate(
        self,
        narrative: AttributeNarrative,
        evidence: ExtractedEvidence,
        *,
        gold_expected_verdict: str,
        attribute_definition: str,
    ) -> JudgeResponse:
        """Return a structured judgement on whether ``narrative``
        truthfully describes the ``evidence`` for its attribute.

        ``gold_expected_verdict`` is the auditor's expected outcome
        from the ToC for this (control, attribute, quarter).
        ``attribute_definition`` is a short text describing what the
        attribute checks (e.g. ``"DC-9.A — preparer signoff"``);
        sourced by the caller from a static per-attribute dict.

        Failure posture: any LLM-side error (empty content, malformed
        JSON, ``JudgeResponse`` validation error) triggers one re-roll
        with the same prompt. On a second failure, this method returns
        ``JudgeResponse(verdict="uncertain", confidence=0.0, ...)``
        with a diagnostic ``reasoning`` and logs WARN. The sweep
        continues regardless.
        """
        prompt = self._render_prompt(
            narrative=narrative,
            evidence=evidence,
            gold_expected_verdict=gold_expected_verdict,
            attribute_definition=attribute_definition,
        )
        return self._invoke_with_fallback(prompt)

    # ---- Helpers ---------------------------------------------------

    def _render_prompt(
        self,
        *,
        narrative: AttributeNarrative,
        evidence: ExtractedEvidence,
        gold_expected_verdict: str,
        attribute_definition: str,
    ) -> str:
        """Render ``judge_v1_0.txt`` against the inputs.

        ``evidence_json`` is built via the generator's static helper so
        the judge sees exactly the per-attribute slice the generator
        saw — same lineage envelope, same attribute_check focus.
        """
        template_text = load_prompt(self._prompt_version)
        evidence_json = NarrativeGenerator._build_evidence_json(narrative.attribute_id, evidence)
        return Template(template_text).substitute(
            narrative_text=narrative.narrative_text,
            cited_fields=json.dumps(narrative.cited_fields),
            evidence_json=evidence_json,
            attribute_definition=attribute_definition,
            gold_expected_verdict=gold_expected_verdict,
        )

    def _invoke_with_fallback(self, prompt: str) -> JudgeResponse:
        """Single retry on LLM-side failure; deterministic uncertain
        fallback on second failure.

        Failure modes the inner loop handles:

        - ``content is None`` — empty response (e.g. ``finish_reason ==
          'length'``). Treated as parse failure.
        - ``json.JSONDecodeError`` — JSON mode occasionally emits
          invalid JSON despite the ``response_format`` constraint.
          Same recovery as the generator: retry once.
        - ``pydantic.ValidationError`` — JSON parsed but
          ``JudgeResponse`` rejected it (e.g. pass/fail with no
          ``cited_evidence_fields``, confidence out of range,
          unknown verdict). Retry once.

        On the second failure of any of the above, log WARN and return
        the uncertain fallback. Never raise.
        """
        for attempt in (1, 2):
            try:
                response = self._client.chat.completions.create(
                    model=self._deployment,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    max_tokens=_MAX_TOKENS,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:  # pragma: no cover — network/auth path
                if attempt == 1:
                    logger.warning("judge LLM call failed on attempt 1 (%s); retrying", exc)
                    continue
                return self._fallback_to_uncertain(f"LLM call failed x2: {exc}")

            self._record_usage_if_present(response)
            content = response.choices[0].message.content
            if not content:
                if attempt == 1:
                    logger.warning(
                        "judge returned empty content on attempt 1 (finish_reason=%r); retrying",
                        response.choices[0].finish_reason,
                    )
                    continue
                return self._fallback_to_uncertain(
                    f"empty content x2 (finish_reason={response.choices[0].finish_reason!r})"
                )

            try:
                payload = json.loads(content)
                return JudgeResponse(**payload)
            except json.JSONDecodeError as exc:
                if attempt == 1:
                    logger.warning("judge JSON parse failed on attempt 1 (%s); retrying", exc.msg)
                    continue
                return self._fallback_to_uncertain(f"JSON parse failure x2: {exc.msg}")
            except ValidationError as exc:
                if attempt == 1:
                    logger.warning("judge response validation failed on attempt 1; retrying")
                    continue
                return self._fallback_to_uncertain(f"validation failure x2: {exc.errors()}")

        # Unreachable — both attempts either return or fall through to
        # the fallback inside the loop. Pylint-pacifier.
        raise RuntimeError("unreachable: _invoke_with_fallback exhausted both attempts")

    @staticmethod
    def _fallback_to_uncertain(reason: str) -> JudgeResponse:
        """Build the deterministic uncertain fallback. Reasoning text
        carries the diagnostic so operators can grep
        ``gold.eval_outcomes`` for failure modes."""
        return JudgeResponse(
            verdict="uncertain",
            confidence=0.0,
            reasoning=f"judge failure x2; falling back to uncertain ({reason})",
            cited_evidence_fields=[],
        )

    def _record_usage_if_present(self, response: Any) -> None:
        """Record per-call token usage on the recorder if one is
        wired. Same shape as ``NarrativeGenerator._record_usage_if_present``
        — recording happens after every billed API call regardless of
        parse outcome (token usage is what the cloud meters, and a
        malformed-JSON response was still a billed call)."""
        if self._usage_recorder is None:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is None or completion_tokens is None:
            return
        self._usage_recorder.record(
            CallUsage(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
            )
        )


__all__ = [
    "Judge",
]
