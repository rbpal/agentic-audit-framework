"""Diagnostic snapshot of audit_dev.gold.narratives.

Reports four signals about the table state — useful after a schema
change, before a sweep, or anytime the live table state is uncertain
(e.g., recovering from an interrupted Terraform apply):

  1. DESCRIBE TABLE       — full column list with types
  2. Row count            — confirms data wasn't wiped
  3. Sample values        — first 3 rows with key columns inspected
                            for type sanity (timestamps, booleans, arrays)
  4. NULL-rate per column — surfaces silent coercion damage

Read-only; no writes, no DDL.

Run via:

    poetry run python scripts/inspect_gold_narratives.py

Requires the three Databricks PAT env vars sourced via:

    source scripts/setup_warehouse_env.sh

This script was created on 2026-05-12 to verify table integrity after
an interrupted Terraform apply. Kept in-tree as the canonical
schema-state diagnostic for ``gold.narratives``. Adapt to other tables
by copying + changing TABLE + SUSPECT_COLUMNS.
"""

from __future__ import annotations

import os
import sys

from databricks import sql  # type: ignore[import-untyped,unused-ignore]

TABLE = "audit_dev.gold.narratives"

# Columns whose presence + types + non-NULL-rate are load-bearing. If
# any of these come back as NULL on every row, the table has been
# damaged by a schema-shift (e.g., a positional rename cascade from
# the databricks_sql_table provider — see TECH_DEBT.md).
SUSPECT_COLUMNS = ("generated_at", "fact_check_passed", "fact_check_issues")


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
        # 1. Column list
        print(f"=== DESCRIBE TABLE {TABLE} ===")
        cur.execute(f"DESCRIBE TABLE {TABLE}")
        rows = cur.fetchall()
        for r in rows:
            print(f"  {r[0]:30s} {r[1]}")
        print()

        # 2. Row count
        cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
        fetched = cur.fetchone()
        assert fetched is not None  # SELECT COUNT(*) always returns one row
        (n_rows,) = fetched
        print(f"=== ROW COUNT: {n_rows} ===")
        if n_rows == 0:
            print("  *** EMPTY TABLE — data may have been wiped ***")
        print()

        # 3. Sample values for the suspect columns
        print("=== SAMPLE VALUES (first 3 rows, suspect columns) ===")
        cols = ", ".join(SUSPECT_COLUMNS)
        try:
            cur.execute(f"SELECT control_id, quarter, attribute_id, {cols} FROM {TABLE} LIMIT 3")
            for r in cur.fetchall():
                scope = f"{r[0]} {r[1]} {r[2]}"
                payload = ", ".join(
                    f"{name}={r[i + 3]!r}" for i, name in enumerate(SUSPECT_COLUMNS)
                )
                print(f"  {scope}: {payload}")
        except Exception as e:
            print(f"  ERROR querying suspect columns: {e}")
            print("  *** Columns may have been renamed/typed wrong ***")
        print()

        # 4. NULL-rate per suspect column
        print("=== NULL counts per suspect column ===")
        for col in SUSPECT_COLUMNS:
            try:
                cur.execute(
                    f"SELECT SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END), COUNT(*) FROM {TABLE}"
                )
                fetched = cur.fetchone()
                assert fetched is not None  # aggregate always returns one row
                (n_null, n_total) = fetched
                marker = "  ⚠️" if n_null and n_null > 0 else "  ✓"
                print(f"{marker} {col}: {n_null or 0} NULL / {n_total} rows")
            except Exception as e:
                print(f"  ✗ {col}: query failed — {e}")
        print()

        # 5. narrative_call_id presence — Step 5 follow-up #5 marker
        has_call_id = any(r[0] == "narrative_call_id" for r in rows)
        print(f"=== narrative_call_id present: {has_call_id} ===")

    print()
    print("=" * 60)
    print("READING THIS OUTPUT:")
    print("  - If row count is 32 and all NULL counts are 0,")
    print("    the table is healthy.")
    print("  - If row count != 32 or any NULL counts > 0, data loss")
    print("    occurred. Recover via Delta time-travel:")
    print(f"        DESCRIBE HISTORY {TABLE}")
    print(f"        SELECT * FROM {TABLE} VERSION AS OF <prior_version>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
