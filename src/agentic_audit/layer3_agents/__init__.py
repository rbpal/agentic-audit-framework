"""Layer 3 multi-agent supervisor package (Step 7).

A LangGraph ``StateGraph`` orchestrating three sub-agents — Extraction,
Validation, Narrative — gated by a rule-based supervisor and the
Step 6 believe-either-fail judge. Fires on Layer-1 exceptions where a
single LLM call can't responsibly judge whether a documented human
justification (IMA amendment, variance explanation) authorises the
anomaly.

This task_01 cut ships the state schema only — no graph wiring, no
LangGraph imports. The ``StateGraph`` skeleton arrives in task_02.

See ``privateDocs/step_07_layer3_multiagent.md`` for the full design.
"""
