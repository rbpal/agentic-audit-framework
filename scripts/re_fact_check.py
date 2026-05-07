"""Re-evaluate fact-check verdicts on existing gold.narratives rows
without re-calling the LLM.

Built for the calibration loop (step_05_task_08): when ``FactChecker``
gains a new stopword, regex tweak, or threshold change, every existing
narrative in ``audit_dev.gold.narratives`` needs its
``fact_check_passed`` and ``fact_check_issues`` columns recomputed —
but the narrative_text itself is fine. Re-running the LLM-touching
sweep would burn tokens and time for zero new information; this
script just re-evaluates the existing 27 (or 32) rows in seconds.

Pipeline per row:

    SELECT (narrative_text, cited_fields, word_count, control, quarter,
            attribute, engagement) FROM gold.narratives
            ↓
    SilverEvidenceReader.read(...)  ← cached per (eng, control, quarter)
            ↓
    FactChecker.check(NarrativeResponse, evidence)
            ↓
    UPDATE gold.narratives SET fact_check_passed = ..., fact_check_issues = ...
           WHERE composite_key

Usage::

    DATABRICKS_HOST=...                                              \\
    DATABRICKS_TOKEN=...                                             \\
    DATABRICKS_SQL_WAREHOUSE_ID=...                                  \\
    poetry run python scripts/re_fact_check.py
                      [--engagement-id alpha-pension-fund-2025]
                      [--prompt-version v1.0]
                      [--dry-run]

No Azure OpenAI credentials needed — this script never touches the LLM.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from agentic_audit.layer2_narrative.fact_checker import FactChecker
from agentic_audit.layer2_narrative.gold_writer import GOLD_NARRATIVES_TABLE
from agentic_audit.layer2_narrative.silver_reader import SilverEvidenceReader
from agentic_audit.models.evidence import ExtractedEvidence
from agentic_audit.models.narrative import NarrativeResponse

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


def _build_warehouse_conn_factory() -> Any:
    """Wire ``databricks.sql.connect`` from env vars."""
    required = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_SQL_WAREHOUSE_ID")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.stderr.write(
            f"ERROR: missing env vars: {', '.join(missing)}\n"
            "       set DATABRICKS_HOST, DATABRICKS_TOKEN, "
            "DATABRICKS_SQL_WAREHOUSE_ID and re-run.\n"
        )
        sys.exit(2)

    from databricks import sql as dbsql  # type: ignore[import-not-found]

    host = os.environ["DATABRICKS_HOST"].removeprefix("https://")
    http_path = f"/sql/1.0/warehouses/{os.environ['DATABRICKS_SQL_WAREHOUSE_ID']}"
    token = os.environ["DATABRICKS_TOKEN"]

    @contextmanager
    def factory() -> Generator[Any, None, None]:
        conn = dbsql.connect(
            server_hostname=host,
            http_path=http_path,
            access_token=token,
        )
        try:
            yield conn
        finally:
            conn.close()

    return factory


_SELECT_NARRATIVES_SQL = f"""
SELECT  control_id,
        quarter,
        attribute_id,
        narrative_text,
        cited_fields,
        word_count,
        fact_check_passed
FROM    {GOLD_NARRATIVES_TABLE}
WHERE   engagement_id = %(engagement_id)s
  AND   prompt_version = %(prompt_version)s
ORDER   BY control_id, quarter, attribute_id
"""

_UPDATE_VERDICT_SQL = f"""
UPDATE {GOLD_NARRATIVES_TABLE}
SET    fact_check_passed  = %(passed)s,
       fact_check_issues  = from_json(%(issues_json)s, 'array<string>')
WHERE  engagement_id   = %(engagement_id)s
  AND  control_id      = %(control_id)s
  AND  quarter         = %(quarter)s
  AND  attribute_id    = %(attribute_id)s
  AND  prompt_version  = %(prompt_version)s
"""


def re_fact_check(
    *,
    engagement_id: str,
    prompt_version: str,
    silver_reader: SilverEvidenceReader,
    fact_checker: FactChecker,
    conn_factory: Any,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Re-evaluate every gold narrative under (engagement, prompt_version).

    Returns ``(n_evaluated, n_passed_after, n_changed_verdict)``.
    Silver evidence is cached per (engagement, control, quarter)
    triple — same posture as the sweep driver — so we hit silver
    8 times, not 32.
    """
    silver_cache: dict[tuple[str, str, str], ExtractedEvidence] = {}
    n_evaluated = 0
    n_passed = 0
    n_changed = 0

    # ─── 1. Load existing narratives ──────────────────────────────
    with conn_factory() as conn, conn.cursor() as cur:
        cur.execute(
            _SELECT_NARRATIVES_SQL,
            {"engagement_id": engagement_id, "prompt_version": prompt_version},
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]

    if not rows:
        print(
            f"No rows found for engagement_id={engagement_id!r}, "
            f"prompt_version={prompt_version!r}. Nothing to re-fact-check."
        )
        return (0, 0, 0)

    print(
        f"Loaded {len(rows)} narratives for "
        f"engagement={engagement_id}, prompt_version={prompt_version}"
    )

    # ─── 2. Re-fact-check + UPDATE ────────────────────────────────
    for row in rows:
        t0 = time.perf_counter()
        cache_key = (engagement_id, row["control_id"], row["quarter"])
        if cache_key not in silver_cache:
            silver_cache[cache_key] = silver_reader.read(
                engagement_id, row["control_id"], row["quarter"]
            )
        evidence = silver_cache[cache_key]

        response = NarrativeResponse(
            narrative_text=row["narrative_text"],
            cited_fields=list(row["cited_fields"]) if row["cited_fields"] else [],
            word_count=row["word_count"],
        )
        verdict = fact_checker.check(response, evidence)

        prior_passed = bool(row["fact_check_passed"])
        verdict_changed = prior_passed != verdict.passed

        n_evaluated += 1
        if verdict.passed:
            n_passed += 1
        if verdict_changed:
            n_changed += 1

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        marker = " [CHANGED]" if verdict_changed else ""
        print(
            f"  {row['control_id']} {row['quarter']} {row['attribute_id']}: "
            f"prior={'PASS' if prior_passed else 'FAIL'} → "
            f"now={'PASS' if verdict.passed else 'FAIL'} "
            f"({len(verdict.issues)} issues){marker} "
            f"({elapsed_ms:.0f} ms)"
        )

        if dry_run:
            continue

        # UPDATE the row in-place. JSON-serialise the issues list and
        # cast in-statement via from_json (same pattern as the writer).
        with conn_factory() as conn, conn.cursor() as cur:
            cur.execute(
                _UPDATE_VERDICT_SQL,
                {
                    "passed": verdict.passed,
                    "issues_json": json.dumps(verdict.issues),
                    "engagement_id": engagement_id,
                    "control_id": row["control_id"],
                    "quarter": row["quarter"],
                    "attribute_id": row["attribute_id"],
                    "prompt_version": prompt_version,
                },
            )

    return n_evaluated, n_passed, n_changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Re-evaluate fact-check verdicts on existing gold.narratives "
            "rows without re-calling the LLM. Used after FactChecker "
            "calibration changes (stopwords, threshold, regex)."
        )
    )
    parser.add_argument(
        "--engagement-id",
        default="alpha-pension-fund-2025",
        help="Engagement to re-evaluate (default: alpha-pension-fund-2025)",
    )
    parser.add_argument(
        "--prompt-version",
        default="v1.0",
        help="Prompt version cohort to re-evaluate (default: v1.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute new verdicts but do not write back to gold.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    factory = _build_warehouse_conn_factory()
    silver_reader = SilverEvidenceReader(factory)
    fact_checker = FactChecker()

    print(
        f"Re-fact-check: engagement={args.engagement_id}, "
        f"prompt_version={args.prompt_version}" + (" (DRY RUN — no writes)" if args.dry_run else "")
    )
    n_evaluated, n_passed, n_changed = re_fact_check(
        engagement_id=args.engagement_id,
        prompt_version=args.prompt_version,
        silver_reader=silver_reader,
        fact_checker=fact_checker,
        conn_factory=factory,
        dry_run=args.dry_run,
    )

    print()
    if n_evaluated == 0:
        return 1
    print(
        f"✓ {n_evaluated} narratives re-evaluated; "
        f"new pass rate {n_passed}/{n_evaluated}; "
        f"{n_changed} verdicts changed" + (" (DRY RUN — no writes)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
