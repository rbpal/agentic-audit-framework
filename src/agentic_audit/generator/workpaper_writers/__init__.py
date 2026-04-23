"""Supporting-workpaper writers (Task 12+).

Each writer module produces one artefact type declared in
``ScenarioSpec.workpapers``. Writers are pure — they take a
``ScenarioSpec`` + ``WorkpaperSpec`` and return an ``openpyxl.Workbook``.
The CLI's ``generate-gold`` orchestrator dispatches on
``workpaper_spec.type`` to the matching writer and handles the disk I/O.
"""

from agentic_audit.generator.workpaper_writers.billing_calc import render_billing_calc

__all__ = ["render_billing_calc"]
