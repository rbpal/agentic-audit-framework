"""Run the three divergence queries (scripts/divergence_summary.sql) against
a given judge_run_id and print the results.

Step 6 task_05 shipped the SQL with :judge_run_id as a bind parameter
designed for the Databricks SQL UI. This script is the operator-side
runner that inlines the bind value, executes each query against the
live warehouse, and prints the results — so re-running the divergence
analysis after a fresh sweep is a single command instead of three
copy-pastes.

Usage:

    poetry run python scripts/run_divergence_summary.py <judge_run_id>

    # e.g.
    poetry run python scripts/run_divergence_summary.py FBFCC5B0A6D48751910CACDD4F9EC011

Reports:

  Q1 — Divergence class summary (5-way): concordant-pass, concordant-fail,
       semantic-only-fail, judge-misses-grounding, judge-uncertain.
       Headline numbers for any sweep writeup.

  Q2 — Non-concordant row detail. Joins gold.narratives via composite key
       (composite-key workaround for the narrative_run_id misnomer — Step 5
       follow-up #5 ships a per-call id that collapses this to a 1:1 join,
       not yet adopted in this query).

  Q3 — Three-way agreement against gold. Cross-tab of (judge_verdict,
       fact_check_verdict) vs gold_expected_verdict. The "right answer
       against gold" view.

Requires DATABRICKS_HOST + DATABRICKS_TOKEN + DATABRICKS_SQL_WAREHOUSE_ID
sourced via scripts/setup_warehouse_env.sh.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from databricks import sql  # type: ignore[import-untyped,unused-ignore]

# ----------------------------------------------------------------------
# Three queries — verbatim copy from scripts/divergence_summary.sql
# with the :judge_run_id bind replaced by a named parameter the
# Databricks SQL driver understands. Keep these in sync with the .sql
# file whenever it changes.
# ----------------------------------------------------------------------

Q1_CLASS_SUMMARY = """
SELECT
    CASE
        WHEN judge_verdict = 'uncertain'                             THEN 'judge-uncertain'
        WHEN judge_verdict = 'pass'  AND fact_check_verdict = 'pass' THEN 'concordant-pass'
        WHEN judge_verdict = 'fail'  AND fact_check_verdict = 'fail' THEN 'concordant-fail'
        WHEN judge_verdict = 'fail'  AND fact_check_verdict = 'pass' THEN 'semantic-only-fail'
        WHEN judge_verdict = 'pass'  AND fact_check_verdict = 'fail' THEN 'judge-misses-grounding'
    END AS divergence_class,
    COUNT(*)                          AS n_rows,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct_of_sweep
FROM   audit_dev.gold.judge_outcomes
WHERE  judge_run_id = %(judge_run_id)s
GROUP  BY 1
ORDER  BY n_rows DESC
"""


Q2_NON_CONCORDANT_DETAIL = """
WITH sweep AS (
    SELECT *
    FROM   audit_dev.gold.judge_outcomes
    WHERE  judge_run_id = %(judge_run_id)s
)
SELECT
    j.engagement_id,
    j.control_id,
    j.quarter,
    j.attribute_id,
    j.judge_verdict,
    j.fact_check_verdict,
    j.gold_expected_verdict,
    j.judge_confidence,
    j.judge_status,
    j.cited_evidence_fields,
    j.judge_reasoning,
    n.narrative_text,
    n.fact_check_issues
FROM   sweep j
LEFT   JOIN audit_dev.gold.narratives n
    ON  n.engagement_id  = j.engagement_id
    AND n.control_id     = j.control_id
    AND n.quarter        = j.quarter
    AND n.attribute_id   = j.attribute_id
    AND (
          -- v1.1+ sweeps: per-call narrative_call_id match
          n.narrative_call_id = j.narrative_run_id
          -- v1.0 historical sweeps: sweep-scoped generation_run_id match
          OR n.generation_run_id = j.narrative_run_id
        )
WHERE  NOT (j.judge_verdict = 'pass' AND j.fact_check_verdict = 'pass')
  AND  NOT (j.judge_verdict = 'fail' AND j.fact_check_verdict = 'fail')
ORDER  BY j.control_id, j.quarter, j.attribute_id
"""


Q3_THREE_WAY_AGREEMENT = """
SELECT
    gold_expected_verdict,
    judge_verdict,
    fact_check_verdict,
    COUNT(*) AS n_rows
FROM   audit_dev.gold.judge_outcomes
WHERE  judge_run_id = %(judge_run_id)s
GROUP  BY 1, 2, 3
ORDER  BY 1, 2, 3
"""


def _detect_prompt_version_for(cur: Any, judge_run_id: str) -> str:
    """Look up the narrative prompt_version this judge sweep evaluated.

    Joins judge_outcomes.narrative_run_id back to a narrative row
    using the dual-path matcher: per-call narrative_call_id for v1.1+
    sweeps (Step 5 follow-up #5), sweep-scoped generation_run_id for
    historical v1.0 sweeps. Either path resolves to exactly one
    narrative row per scope tuple, so the DISTINCT collapses to one
    prompt_version per uniform sweep.
    """
    cur.execute(
        """
        SELECT DISTINCT n.prompt_version
        FROM   audit_dev.gold.judge_outcomes j
        JOIN   audit_dev.gold.narratives n
          ON  n.engagement_id  = j.engagement_id
          AND n.control_id     = j.control_id
          AND n.quarter        = j.quarter
          AND n.attribute_id   = j.attribute_id
          AND (
                n.narrative_call_id  = j.narrative_run_id
             OR n.generation_run_id  = j.narrative_run_id
          )
        WHERE  j.judge_run_id = %(judge_run_id)s
        """,
        {"judge_run_id": judge_run_id},
    )
    versions = [r[0] for r in cur.fetchall()]
    if not versions:
        raise SystemExit(
            f"no rows in audit_dev.gold.judge_outcomes for judge_run_id={judge_run_id!r}; "
            "is the id correct?"
        )
    if len(versions) > 1:
        # Multi-version sweep is possible in principle but not expected
        # — flag it and use the first as a best-effort.
        print(f"⚠️  multiple prompt_versions {versions} match this judge_run_id", file=sys.stderr)
    return str(versions[0])


def _print_rows(rows: list, headers: list[str]) -> None:
    if not rows:
        print("  (no rows)")
        return
    # Compute column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*[str(v) for v in row]))


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        print(
            "\nERROR: exactly one positional arg required (judge_run_id).",
            file=sys.stderr,
        )
        return 2
    judge_run_id = args[0]

    required = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_SQL_WAREHOUSE_ID")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR — missing env vars: {missing}", file=sys.stderr)
        return 1

    with (
        sql.connect(
            server_hostname=os.environ["DATABRICKS_HOST"],
            http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_SQL_WAREHOUSE_ID']}",
            access_token=os.environ["DATABRICKS_TOKEN"],
        ) as conn,
        conn.cursor() as cur,
    ):
        prompt_version = _detect_prompt_version_for(cur, judge_run_id)
        print(f"judge_run_id     = {judge_run_id}")
        print(f"prompt_version   = {prompt_version}")
        print()

        # Q1
        print("=" * 78)
        print("Q1 — Divergence class summary")
        print("=" * 78)
        cur.execute(Q1_CLASS_SUMMARY, {"judge_run_id": judge_run_id})
        rows = cur.fetchall()
        _print_rows(rows, ["divergence_class", "n_rows", "pct_of_sweep"])
        print()

        # Q2 — join uses narrative_call_id / generation_run_id (dual-path)
        # so no prompt_version param needed.
        print("=" * 78)
        print("Q2 — Non-concordant row detail (judge ≠ fact-check)")
        print("=" * 78)
        cur.execute(Q2_NON_CONCORDANT_DETAIL, {"judge_run_id": judge_run_id})
        rows = cur.fetchall()
        if not rows:
            print("  (no non-concordant rows — judge and fact-check agree on every row)")
        else:
            for i, row in enumerate(rows, 1):
                (
                    engagement,
                    control,
                    quarter,
                    attribute,
                    judge_v,
                    fc_v,
                    gold_v,
                    confidence,
                    status,
                    cited,
                    reasoning,
                    narrative_text,
                    fc_issues,
                ) = row
                # `cited` and `fc_issues` arrive as numpy.ndarray from the
                # Databricks SQL driver for array<string> columns; testing
                # them with `if x else []` triggers the
                # "truth value of an array is ambiguous" ValueError.
                # Compare to None explicitly.
                cited_list = list(cited) if cited is not None else []
                issues_list = list(fc_issues) if fc_issues is not None else []
                print(
                    f"  [{i}] {control} {quarter} {attribute}: "
                    f"judge={judge_v} (conf={confidence}, status={status}), "
                    f"fc={fc_v}, gold={gold_v}"
                )
                print(f"      cited_evidence_fields: {cited_list}")
                print(f"      fact_check_issues:     {issues_list}")
                print(f"      judge_reasoning:       {reasoning}")
                print(f"      narrative_text:        {narrative_text}")
                print()

        # Q3
        print("=" * 78)
        print("Q3 — Three-way agreement against gold (judge × fact_check × gold)")
        print("=" * 78)
        cur.execute(Q3_THREE_WAY_AGREEMENT, {"judge_run_id": judge_run_id})
        rows = cur.fetchall()
        _print_rows(
            rows,
            ["gold_expected_verdict", "judge_verdict", "fact_check_verdict", "n_rows"],
        )
        print()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
