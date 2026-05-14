"""Layer 3 Extraction sub-agent (Step 7 task_04).

Wraps LangGraph 1.x's ``create_react_agent`` with the three placeholder
tools from ``tools.py``, a per-``exception_type`` prompt template, and
pydantic structured output (``ExtractionFindings``). The supervisor
injects an instance via ``config["configurable"]["layer3_extraction_agent"]``
and ``extraction_agent_node`` runs ``.invoke(state)`` to populate
``InvestigationState.extraction_findings``.

Design choices (mirrors Layer 2's ``Judge``):

- **Managed-identity auth** through ``langchain-openai``'s
  ``AzureChatOpenAI`` with an ``azure_ad_token_provider`` from
  ``DefaultAzureCredential``. Same auth chain as the generator + judge;
  no API keys.
- **Lazy LLM import + lazy agent build.** The ReAct agent is compiled
  on first ``.invoke()`` so test fixtures that inject a fake agent
  never pay the construction cost. Production wiring builds once per
  ``ExtractionAgent`` instance and reuses the compiled graph across
  every investigation in a sweep.
- **Prompt-version pinning.** Each ``ExtractionAgent`` holds one
  ``prompt_version`` (e.g. ``"v1.0"``) and renders the two
  exception-type variants by appending the type to the filename. A
  future v1.1 prompt cycle bumps the version once and Step 7 picks up
  both variants atomically.
- **String.Template substitution**, not f-strings — prompts contain
  literal ``{ }`` for the JSON example, same constraint that drove the
  Layer 2 generator's choice.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agentic_audit.layer3_agents.state import (
    ExceptionType,
    ExtractionFindings,
    InvestigationState,
)
from agentic_audit.layer3_agents.tools import (
    compare_billing_rates,
    read_billing_rate,
    read_reviewer_comments,
)
from agentic_audit.models.telemetry import CallUsage, UsageRecorder
from agentic_audit.observability import traced_function

if TYPE_CHECKING:
    from langchain_openai import AzureChatOpenAI

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Same scope the Layer-2 generator + judge use. Pinning explicitly
# rather than importing from layer2_narrative avoids a top-level
# dependency on Layer 2 — Layer 3 has its own auth flow even though
# the tenant + scope are identical.
_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"

# Matches the Layer-2 generator / judge API version pin. Bumped in
# lockstep when a tenant-wide upgrade lands.
AZURE_OPENAI_API_VERSION = "2024-10-21"


def _build_azure_chat_openai(
    *,
    endpoint: str,
    deployment: str,
    api_version: str = AZURE_OPENAI_API_VERSION,
    temperature: float = 0.0,
) -> AzureChatOpenAI:
    """Build a managed-identity-backed ``AzureChatOpenAI`` client.

    Mirrors ``layer2_narrative.generator._build_azure_openai_client``
    one step up the stack — ``AzureChatOpenAI`` is the LangChain wrapper
    ``create_react_agent`` consumes, not the bare ``AzureOpenAI`` client
    Layer 2 uses for single-shot completions.
    """
    # Lazy import — keeps the module loadable in tests that inject a
    # fake agent without the azure-identity wheel installed.
    from azure.identity import (  # noqa: PLC0415
        DefaultAzureCredential,
        get_bearer_token_provider,
    )
    from langchain_openai import AzureChatOpenAI  # noqa: PLC0415

    token_provider = get_bearer_token_provider(DefaultAzureCredential(), _COGNITIVE_SERVICES_SCOPE)
    return AzureChatOpenAI(
        azure_endpoint=endpoint,
        azure_deployment=deployment,
        api_version=api_version,
        azure_ad_token_provider=token_provider,
        temperature=temperature,
    )


class ExtractionAgent:
    """LangGraph ReAct agent specialised for Layer-3 fact extraction.

    Pinned to a deployment + prompt version at construction; renders
    the per-``exception_type`` prompt against the live investigation
    state, runs the ReAct loop with the three placeholder tools, and
    parses the structured output back into ``ExtractionFindings``.

    The agent itself is built lazily on first ``invoke``. Tests that
    inject a fake never trigger the build; production wiring builds
    once per instance and reuses across every investigation in a sweep.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str = "gpt-4o",
        prompt_version: str = "v1.0",
        client: AzureChatOpenAI | None = None,
        api_version: str = AZURE_OPENAI_API_VERSION,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        """Build an Extraction sub-agent.

        ``client`` is dependency-injected for tests; in production it is
        ``None`` and the constructor builds one via
        ``_build_azure_chat_openai`` on first use. ``prompt_version``
        is the suffix that resolves to the per-exception-type prompt
        files — ``"v1.0"`` reads ``extraction_v1_0_billing_rate_change.txt``
        and ``extraction_v1_0_variance_plausibility.txt``.

        ``usage_recorder`` is optional; when supplied, every LLM call
        the ReAct loop makes (typically 2–4 per invocation: tool-call
        decisions + structured-response generation) records its token
        usage on the recorder via a LangChain ``BaseCallbackHandler``.
        Same recorder is shared with Validation + Narrative when wired
        through ``run_investigation``, producing one aggregate cost-
        telemetry row per investigation.
        """
        self._endpoint = endpoint
        self._deployment = deployment
        self._prompt_version = prompt_version
        self._api_version = api_version
        # ``_client`` is the LangChain ChatModel; ``_compiled`` is the
        # ReAct agent built from it. Both populate on first invoke.
        self._client = client
        self._compiled: Any = None
        self._usage_recorder = usage_recorder

    @classmethod
    def from_env(
        cls,
        *,
        deployment: str = "gpt-4o",
        prompt_version: str = "v1.0",
        endpoint_env_var: str = "AZURE_OPENAI_ENDPOINT",
        usage_recorder: UsageRecorder | None = None,
    ) -> ExtractionAgent:
        """Build from ``AZURE_OPENAI_ENDPOINT``. Same env contract as
        ``Judge.from_env`` and ``NarrativeGenerator.from_env``."""
        endpoint = os.environ.get(endpoint_env_var)
        if not endpoint:
            raise RuntimeError(
                f"environment variable {endpoint_env_var!r} is not set; "
                "either export it (e.g. "
                "https://aoai-aaf-rbpal-dev.openai.azure.com/) or pass "
                "endpoint explicitly to ExtractionAgent(...)."
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

    # ── Production entry point ──────────────────────────────────────

    @traced_function("layer3.extraction_agent.invoke")
    def invoke(self, state: InvestigationState) -> ExtractionFindings:
        """Run one extraction pass against the live investigation state.

        Selects the prompt by ``state["exception_type"]``, renders it
        with the investigation scope (engagement, control, attribute,
        current + prior quarter), and runs the ReAct agent with the
        three placeholder tools. The agent's structured response is
        validated against ``ExtractionFindings`` at the boundary; a
        malformed response surfaces as ``ValidationError`` rather than
        silently propagating empty findings.
        """
        findings, _messages = self._invoke_with_messages(state)
        return findings

    def _invoke_with_messages(
        self, state: InvestigationState
    ) -> tuple[ExtractionFindings, list[Any]]:
        """Diagnostic variant of ``invoke`` — returns findings PLUS the
        full ReAct message history (HumanMessage / AIMessage /
        ToolMessage chain).

        Production callers should use ``invoke`` which discards the
        history. The history is the auditability artefact for "what
        did the agent reason about" — task_08 may persist it via
        ``@traced_function`` spans; until then this hook lets the
        slow integration test surface the reasoning chain for
        operator review.

        Leading underscore signals "diagnostic, not a public API" —
        the signature can change without bumping the production
        contract.
        """
        exception_type = state["exception_type"]
        prompt = self._render_prompt(state, exception_type)
        agent = self._ensure_compiled()

        invoke_config: dict[str, Any] = {}
        if self._usage_recorder is not None:
            invoke_config["callbacks"] = [_UsageRecordingCallbackHandler(self._usage_recorder)]

        result: dict[str, Any] = agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=invoke_config or None,
        )
        findings = self._parse_structured_response(result)
        messages: list[Any] = result.get("messages", [])
        return findings, messages

    # ── Prompt rendering ────────────────────────────────────────────

    def _render_prompt(
        self,
        state: InvestigationState,
        exception_type: ExceptionType,
    ) -> str:
        """Render the per-exception-type prompt template.

        The prompt files live under ``prompts/`` and follow the
        convention ``extraction_<version>_<exception_type>.txt`` —
        version dots are replaced by underscores (``"v1.0"`` →
        ``"v1_0"``) to match the Layer 2 prompt loader convention.
        """
        filename = f"extraction_{self._prompt_version.replace('.', '_')}_{exception_type}.txt"
        template_text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        prior_quarter = state["prior_quarter_evidence"].quarter
        return Template(template_text).substitute(
            engagement_id=state["engagement_id"],
            control_id=state["control_id"],
            attribute_id=state["attribute_id"],
            quarter=state["quarter"],
            prior_quarter=prior_quarter,
        )

    # ── Agent build ─────────────────────────────────────────────────

    def _ensure_compiled(self) -> Any:
        """Lazy ReAct-agent construction.

        Building ``create_react_agent`` requires a live
        ``AzureChatOpenAI`` client which in turn requires
        ``azure-identity`` — both deferred to first call so tests that
        inject a fake never trigger the dependency chain. Subsequent
        calls reuse the cached compile."""
        if self._compiled is not None:
            return self._compiled

        if self._client is None:
            self._client = _build_azure_chat_openai(
                endpoint=self._endpoint,
                deployment=self._deployment,
                api_version=self._api_version,
            )

        # Lazy import to keep ``langgraph.prebuilt`` off the module-
        # import hot path; tests that don't touch the LLM never pay
        # the import cost.
        from langgraph.prebuilt import create_react_agent  # noqa: PLC0415

        self._compiled = create_react_agent(
            model=self._client,
            tools=[read_billing_rate, compare_billing_rates, read_reviewer_comments],
            response_format=ExtractionFindings,
        )
        return self._compiled

    # ── Response parsing ────────────────────────────────────────────

    @staticmethod
    def _parse_structured_response(result: dict[str, Any]) -> ExtractionFindings:
        """Pull the ``ExtractionFindings`` out of the agent result.

        ``create_react_agent`` with ``response_format=PydanticModel``
        populates ``result["structured_response"]`` with an already-
        validated instance. We re-validate defensively because a
        future LangGraph minor could change the key, the validation
        posture, or return the raw dict instead — and a malformed
        structured response routing into the supervisor would silently
        corrupt the ``gold.layer3_decisions`` row.
        """
        structured = result.get("structured_response")
        if structured is None:
            raise ValueError(
                "ExtractionAgent: agent.invoke result has no "
                "'structured_response' key. LangGraph response_format "
                "wiring may have changed; check create_react_agent docs."
            )
        if isinstance(structured, ExtractionFindings):
            return structured
        # Defensive re-validation in case the agent returned a dict.
        if isinstance(structured, dict):
            try:
                return ExtractionFindings(**structured)
            except ValidationError as e:
                logger.exception("ExtractionAgent structured_response failed validation")
                raise ValueError(
                    f"ExtractionAgent: structured_response did not parse as ExtractionFindings: {e}"
                ) from e
        raise TypeError(
            f"ExtractionAgent: unexpected structured_response type "
            f"{type(structured).__name__}; expected ExtractionFindings or dict."
        )

    # ── Test utility ────────────────────────────────────────────────

    def _peek_rendered_prompt(self, state: InvestigationState) -> str:
        """Test-only accessor — renders the prompt without invoking
        the agent. Exposed under a leading underscore to signal
        non-production status; the only caller is unit tests in
        ``tests/unit/layer3_agents/test_extraction_agent.py``."""
        return self._render_prompt(state, state["exception_type"])


# LangChain callback that captures per-LLM-call token usage and
# pushes it onto a UsageRecorder. Imports the LangChain base class
# lazily inside the closure-class definition so the module remains
# importable in test environments where langchain_core isn't fully
# resolvable yet (the lazy-import pattern matches
# ``_build_azure_chat_openai`` above).
class _UsageRecordingCallbackHandler:
    """LangChain callback that pushes per-LLM-call token usage onto a
    ``UsageRecorder``.

    LangChain's ``LLMResult.llm_output`` carries a ``"token_usage"``
    dict (``prompt_tokens`` + ``completion_tokens``) for every Azure
    OpenAI completion the ReAct loop makes. Subclassing
    ``BaseCallbackHandler`` lazily (via ``__init_subclass__``-style
    delayed binding through ``_get_base``) keeps the module importable
    in tests that never instantiate the handler.

    Same recorder shape as the raw-openai-client agents
    (``ValidationAgent``, ``NarrativeAgent``, Layer-2 ``Judge``) — all
    three sub-agents in one investigation share one recorder so the
    cost-telemetry row aggregates the full ReAct + single-call
    spend.
    """

    def __init__(self, recorder: UsageRecorder) -> None:
        # Lazy import of the LangChain base — not at module import.
        from langchain_core.callbacks import BaseCallbackHandler  # noqa: PLC0415

        # Dynamically rebase to the LangChain class so callback
        # dispatch picks up our overridden ``on_llm_end``. The hack:
        # we can't subclass at class-definition time without forcing
        # langchain_core import on every module load.
        self.__class__ = type(
            self.__class__.__name__,
            (BaseCallbackHandler,),
            dict(self.__class__.__dict__),
        )
        self._recorder = recorder

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """Pull token usage off the LLMResult and record it.

        ``LLMResult.llm_output`` is dict-shaped per the LangChain
        contract; ``token_usage`` is the standard key Azure OpenAI
        populates. Defensive against missing keys — silently degrades
        to "no record" rather than raising into the agent loop.
        """
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if prompt_tokens is None or completion_tokens is None:
            return
        self._recorder.record(
            CallUsage(
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
            )
        )


# Helper for the smoke logger — keeps log lines structured without
# pulling in a formatter library.
def _summarise_findings(findings: ExtractionFindings) -> str:
    return json.dumps(
        findings.model_dump(exclude_none=True),
        default=str,
        sort_keys=True,
    )


__all__ = [
    "AZURE_OPENAI_API_VERSION",
    "PROMPTS_DIR",
    "ExtractionAgent",
]
