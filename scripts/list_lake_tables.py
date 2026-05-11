"""Lake snapshot — list every table in audit_dev.{bronze, silver, gold}
with its row count.

Operator diagnostic for "what's currently in the lake?" — useful before
running a sweep (to confirm seed data exists) or after a Terraform
apply (to confirm new tables landed). Read-only; no writes, no DDL.

Run via:

    poetry run python scripts/list_lake_tables.py

Requires the three Databricks PAT env vars sourced via:

    source scripts/setup_warehouse_env.sh

Output shape (counts vary by what's been written):

    === audit_dev.bronze ===
      tocs_raw                            8 rows
      workpapers_raw                    152 rows

    === audit_dev.silver ===
      ...

    === audit_dev.gold ===
      narratives                         32 rows
      judge_outcomes                     32 rows
      cost_telemetry                      2 rows
      ...
"""

from __future__ import annotations

import os
import sys

from databricks import sql  # type: ignore[import-untyped,unused-ignore]

CATALOG = "audit_dev"
SCHEMAS = ("bronze", "silver", "gold")


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
        for schema in SCHEMAS:
            print(f"=== {CATALOG}.{schema} ===")
            try:
                cur.execute(f"SHOW TABLES IN {CATALOG}.{schema}")
                tables = cur.fetchall()
            except Exception as e:
                print(f"  ✗ SHOW TABLES failed: {e}")
                print()
                continue

            if not tables:
                print("  (no tables)")
                print()
                continue

            # SHOW TABLES returns (database, tableName, isTemporary, ...).
            # Sort alphabetically so the output is stable across runs.
            for row in sorted(tables, key=lambda r: r[1]):
                table_name = row[1]
                fqn = f"{CATALOG}.{schema}.{table_name}"
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {fqn}")
                    fetched = cur.fetchone()
                    assert fetched is not None  # SELECT COUNT(*) always returns one row
                    (n_rows,) = fetched
                    print(f"  {table_name:30s} {n_rows:>6} rows")
                except Exception as e:
                    print(f"  {table_name:30s} <count failed: {type(e).__name__}>")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
