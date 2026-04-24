"""Synthetic workbook generation for the agentic audit framework.

v2 (Step 2 cutover): engagement-level writers live in
``agentic_audit.generator.engagement_writers``. The ``content_hash``
helper is control-agnostic and stays at this level.
"""

from agentic_audit.generator.content_hash import content_hash

__all__ = ["content_hash"]
