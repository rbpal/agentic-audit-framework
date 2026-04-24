"""v2 engagement-level writers — per-quarter DC-9 / DC-2 W/Ps + engagement TOC.

Each module emits one artefact family. All writers take an
``EngagementSpec`` (the whole-engagement context) + a ``Quarter``; they
read the declared defect for that (control, quarter) pair via
``quarter_control`` and shape their output accordingly.

See ``privateDocs/step_02_corpus_v2.md`` §6–§7 for the per-attribute
data layout. v1's scenario-scoped writers in
``agentic_audit.generator.workpaper_writers`` remain in place until
the final cutover commit on this branch.
"""

from agentic_audit.generator.engagement_writers.dc9 import render_dc9_quarter

__all__ = ["render_dc9_quarter"]
