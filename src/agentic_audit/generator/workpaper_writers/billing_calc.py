"""Billing-calculation supporting workpaper — DC-9 scenarios.

A minimal 2-column workbook (``label`` | ``value``) documenting the
billing computation the TOC claims to have tied out. Emits fields a
real auditor would expect to see in a supporting schedule:

* Entity name
* Period (quarter + year)
* Asset value
* Billing rate
* Billing fee = assets × rate
* Preparer initials + date

The billing numbers (asset value, rate, fee) come from the shared
``fake_data.compute_canonical_billing(spec)`` so the TOC and the W/P
agree on the "actual" values. For ``figure_mismatch`` scenarios the
TOC's *claimed* fee diverges from this canonical fee — that's the
Task-13 cross-file inconsistency the agent must detect.
"""

from __future__ import annotations

import random

from openpyxl import Workbook

from agentic_audit.generator import fake_data
from agentic_audit.models.scenario import ScenarioSpec, WorkpaperSpec


def render_billing_calc(spec: ScenarioSpec, wp_spec: WorkpaperSpec) -> Workbook:
    """Produce a billing-calculation ``.xlsx`` workbook.

    Returns an openpyxl Workbook with a single sheet named ``Billing Calc``.
    Byte-deterministic given the same ``(spec, wp_spec)`` pair.
    """
    # Per-writer rng (seeded, namespaced independently of the billing
    # triple) — drives the free-text fields (entity, date, preparer)
    # without touching the canonical-billing computation.
    rng = random.Random(f"{spec.seed}_billing_calc_writer")

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Billing Calc"

    entity = fake_data.fake_entity_name(rng)
    year_end = fake_data.fake_year_end_date(rng, spec.quarter)
    period_str = f"{spec.quarter} {year_end.year}"
    asset_value, rate_str, billing_fee = fake_data.compute_canonical_billing(spec)
    preparer_initials = fake_data.fake_initials(rng)
    prep_date = fake_data.fake_year_end_date(rng, spec.quarter)

    rows: list[tuple[str, object | None]] = [
        (f"Billing Calculation — {wp_spec.toc_reference_code}", None),
        ("Entity", entity),
        ("Period", period_str),
        ("Asset value (USD)", asset_value),
        ("Billing rate", rate_str),
        ("Billing fee (USD)", billing_fee),
        ("Prepared by", preparer_initials),
        ("Date prepared", prep_date.isoformat()),
    ]
    for r, (label, value) in enumerate(rows, start=1):
        ws.cell(row=r, column=1, value=label)
        if value is not None:
            ws.cell(row=r, column=2, value=value)

    return wb
