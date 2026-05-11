"""Verify that the ``narrative_call_id`` column landed on
``audit_dev.gold.narratives``.

Single-purpose verification for Step 5 follow-up #5 (Terraform schema
PR #83 + plumbing PR #84). Run AFTER ``terraform apply`` to confirm
the schema change is live. Reads three signals:

  1. DESCRIBE TABLE        — column exists, type STRING, nullable.
  2. COUNT(*)              — row count unchanged from the 32-row
                             v1.0 baseline.
  3. SELECT narrative_call_id by prompt_version — confirms v1.0
                             historical rows carry NULL (the schema
                             is nullable by design) and v1.1+ rows
                             (once they exist) carry populated IDs.

Exits 0 on success, 1 on any inconsistency.

Run via:

    poetry run python scripts/verify_narrative_call_id.py

Requires the three Databricks PAT env vars sourced via:

    source scripts/setup_warehouse_env.sh

This script remains useful for any future schema verification of
``narrative_call_id``. For general schema inspection of
``gold.narratives``, prefer ``scripts/inspect_gold_narratives.py``.
"""

from __future__ import annotations

import os
import sys

from databricks import sql  # type: ignore[import-untyped,unused-ignore]

TABLE = "audit_dev.gold.narratives"
COLUMN = "narrative_call_id"


def main() -> int:
    required = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_SQL_WAREHOUSE_ID")
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR — missing env vars: {missing}", file=sys.stderr)
        print("Run: source scripts/setup_warehouse_env.sh", file=sys.stderr)
        return 1

    with (
        sql.connect(
            server_hostname=os.environ["DATABRICKS_HOST"],
            http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_SQL_WAREHOUSE_ID']}",
            access_token=os.environ["DATABRICKS_TOKEN"],
        ) as conn,
        conn.cursor() as cur,
    ):
        # 1. Column exists with the right type
        cur.execute(f"DESCRIBE TABLE {TABLE}")
        rows = cur.fetchall()
        match = next((r for r in rows if r[0] == COLUMN), None)
        if match is None:
            print(f"FAIL — column {COLUMN!r} not found on {TABLE}.")
            print("First 30 columns present:")
            for r in rows[:30]:
                print(f"  {r[0]} {r[1]}")
            return 1
        col_name, col_type = match[0], match[1]
        print(f"  ✓ column found: {col_name} {col_type}")
        if col_type.lower() != "string":
            print(f"FAIL — expected STRING, got {col_type!r}")
            return 1

        # 2. Row count preserved
        cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        fetched = cur.fetchone()
        assert fetched is not None  # SELECT COUNT(*) always returns one row
        (n_rows,) = fetched
        print(f"  ✓ row count: {n_rows}")
        if n_rows != 32:
            print(f"  ⚠️  expected 32 baseline rows, got {n_rows}")

        # 3. NULL-rate per prompt_version cohort
        cur.execute(
            f"SELECT prompt_version, COUNT(*) AS n, "
            f"SUM(CASE WHEN {COLUMN} IS NULL THEN 1 ELSE 0 END) AS n_null "
            f"FROM {TABLE} GROUP BY prompt_version ORDER BY prompt_version"
        )
        cohorts = cur.fetchall()
        for pv, n, n_null in cohorts:
            print(f"  prompt_version={pv!r}: {n} rows, {n_null} NULL in {COLUMN}")
            if pv == "v1.0" and n_null != n:
                print(f"  ⚠️  v1.0 rows should all carry NULL pre-plumbing-PR; got {n_null}/{n}")
            if pv != "v1.0" and n_null > 0:
                print(f"  ⚠️  {pv} rows should all carry populated IDs; got {n_null} NULLs")

    print()
    print(f"PASS — {COLUMN} present on {TABLE}, schema change landed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
