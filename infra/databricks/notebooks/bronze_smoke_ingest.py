# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze smoke ingest (step_03_task_09)
# MAGIC
# MAGIC Lands the Step-1 corpus into `audit_dev.bronze.workpapers_raw` and
# MAGIC `audit_dev.bronze.tocs_raw`, then runs the lineage-verification queries
# MAGIC defined in `infra/databricks/sql/verify_bronze_smoke.sql`.
# MAGIC
# MAGIC **Prerequisites (one-time per environment):**
# MAGIC 1. Step-1 corpus uploaded to the bronze external location:
# MAGIC    `abfss://bronze@<storage_account>.dfs.core.windows.net/corpus/v2/`
# MAGIC    with subfolders `workpapers/` and `tocs/`. A simple `az storage blob
# MAGIC    upload-batch` from the local `eval/gold_scenarios/` folder is enough.
# MAGIC 2. The notebook must run as a principal with `MODIFY` on
# MAGIC    `audit_dev.bronze.*` and `READ FILES` on the bronze external location.
# MAGIC
# MAGIC **What this notebook does:**
# MAGIC * Discovers `dc{n}_Q{q}_ref.xlsx` and `dc{n}_Q{q}.json` files under the
# MAGIC   corpus root.
# MAGIC * Calls `agentic_audit.ingest.bronze_smoke` to extract typed records.
# MAGIC * Writes them as Delta `MERGE` operations keyed on `file_hash` — so
# MAGIC   re-running is idempotent.
# MAGIC * Runs the verification queries inline.
# MAGIC
# MAGIC **What this notebook deliberately does not do:**
# MAGIC * Land PDFs into `bronze.raw_pdfs` — the Step-1 corpus has no PDFs.
# MAGIC * Drive the silver-tier ETL — that's `step_04`.

# COMMAND ----------

# MAGIC %pip install openpyxl
# dbutils.library.restartPython()  # uncomment on first run after a new cluster

# COMMAND ----------

import os

# The agentic_audit package is expected to be available either via a wheel
# attached to the cluster or via `%pip install -e <repo-root>`. Adjust the
# path below to the repo checkout location on the cluster.
import sys
from datetime import UTC, datetime
from pathlib import Path

from pyspark.sql import Row

sys.path.insert(0, "/Workspace/Repos/agentic-audit-framework/src")

from agentic_audit.ingest.bronze_smoke import (
    discover_corpus,
    extract_toc_record,
    extract_workpaper_rows,
)

# COMMAND ----------

CORPUS_ROOT = Path(os.environ.get("CORPUS_ROOT", "/Volumes/audit_dev/bronze/raw_pdfs/corpus/v2"))
ENGAGEMENT_ID = "alpha-pension-fund-2025"
INGESTED_BY = os.environ.get("USER", "smoke-ingest")
INGESTED_AT = datetime.now(UTC)

print(f"corpus_root  = {CORPUS_ROOT}")
print(f"engagement   = {ENGAGEMENT_ID}")
print(f"ingested_by  = {INGESTED_BY}")
print(f"ingested_at  = {INGESTED_AT.isoformat()}")

# COMMAND ----------

workpapers, tocs = discover_corpus(CORPUS_ROOT)
print(f"discovered {len(workpapers)} workpapers, {len(tocs)} tocs")
assert len(workpapers) == 8 and len(tocs) == 8, "expected 8 + 8 (DC-2/DC-9 × Q1-Q4)"

# COMMAND ----------

# MAGIC %md ## Workpapers → bronze.workpapers_raw

# COMMAND ----------

wp_records = []
for path in workpapers:
    wp_records.extend(
        extract_workpaper_rows(
            path,
            engagement_id=ENGAGEMENT_ID,
            ingested_by=INGESTED_BY,
            ingested_at=INGESTED_AT,
        )
    )
print(f"extracted {len(wp_records)} workpaper rows")

wp_df = spark.createDataFrame(  # type: ignore[name-defined]  # spark provided by Databricks
    [
        Row(
            source_path=r.source_path,
            file_hash=r.file_hash,
            engagement_id=r.engagement_id,
            sheet_name=r.sheet_name,
            row_index=r.row_index,
            raw_data=r.raw_data,
            ingested_at=r.ingested_at,
            ingested_by=r.ingested_by,
        )
        for r in wp_records
    ]
)
wp_df.createOrReplaceTempView("_wp_staged")

# COMMAND ----------

# MERGE on (file_hash, sheet_name, row_index) — re-running the notebook is a
# no-op if the file hasn't changed. If the file is replaced (different hash),
# the new rows insert alongside the old; nothing in bronze is ever updated or
# deleted by ingest itself.
spark.sql(  # type: ignore[name-defined]
    """
    MERGE INTO audit_dev.bronze.workpapers_raw AS t
    USING _wp_staged AS s
       ON  t.file_hash = s.file_hash
       AND t.sheet_name = s.sheet_name
       AND t.row_index = s.row_index
    WHEN NOT MATCHED THEN INSERT (
        source_path, file_hash, engagement_id, sheet_name,
        row_index, raw_data, ingested_at, ingested_by
    ) VALUES (
        s.source_path, s.file_hash, s.engagement_id, s.sheet_name,
        s.row_index, s.raw_data, s.ingested_at, s.ingested_by
    )
    """
)
display(spark.sql("SELECT COUNT(*) AS rowcount FROM audit_dev.bronze.workpapers_raw"))  # type: ignore[name-defined]

# COMMAND ----------

# MAGIC %md ## TOCs → bronze.tocs_raw

# COMMAND ----------

toc_records = [
    extract_toc_record(
        path,
        engagement_id=ENGAGEMENT_ID,
        ingested_by=INGESTED_BY,
        ingested_at=INGESTED_AT,
    )
    for path in tocs
]
print(f"extracted {len(toc_records)} toc records")

toc_df = spark.createDataFrame(  # type: ignore[name-defined]
    [
        Row(
            source_path=r.source_path,
            file_hash=r.file_hash,
            engagement_id=r.engagement_id,
            control_id=r.control_id,
            quarter=r.quarter,
            raw_json=r.raw_json,
            ingested_at=r.ingested_at,
            ingested_by=r.ingested_by,
        )
        for r in toc_records
    ]
)
toc_df.createOrReplaceTempView("_toc_staged")

# COMMAND ----------

spark.sql(  # type: ignore[name-defined]
    """
    MERGE INTO audit_dev.bronze.tocs_raw AS t
    USING _toc_staged AS s
       ON t.file_hash = s.file_hash
    WHEN NOT MATCHED THEN INSERT (
        source_path, file_hash, engagement_id, control_id,
        quarter, raw_json, ingested_at, ingested_by
    ) VALUES (
        s.source_path, s.file_hash, s.engagement_id, s.control_id,
        s.quarter, s.raw_json, s.ingested_at, s.ingested_by
    )
    """
)
display(spark.sql("SELECT COUNT(*) AS rowcount FROM audit_dev.bronze.tocs_raw"))  # type: ignore[name-defined]

# COMMAND ----------

# MAGIC %md ## Lineage verification
# MAGIC
# MAGIC Mirrors `infra/databricks/sql/verify_bronze_smoke.sql`. Failures here
# MAGIC fail the notebook — re-run after fixing whatever caused the drift.

# COMMAND ----------

# 1. Distinct (file_hash) count matches the number of files we ingested.
wp_hash_count = spark.sql(  # type: ignore[name-defined]
    "SELECT COUNT(DISTINCT file_hash) AS n FROM audit_dev.bronze.workpapers_raw"
).collect()[0]["n"]
toc_hash_count = spark.sql(  # type: ignore[name-defined]
    "SELECT COUNT(DISTINCT file_hash) AS n FROM audit_dev.bronze.tocs_raw"
).collect()[0]["n"]
assert wp_hash_count == 8, f"expected 8 distinct workpaper hashes, got {wp_hash_count}"
assert toc_hash_count == 8, f"expected 8 distinct toc hashes, got {toc_hash_count}"

# 2. Every (control_id, quarter) pair from the manifest is present in tocs_raw.
pairs = spark.sql(  # type: ignore[name-defined]
    "SELECT control_id, quarter FROM audit_dev.bronze.tocs_raw ORDER BY control_id, quarter"
).collect()
got = {(r["control_id"], r["quarter"]) for r in pairs}
expected = {(c, q) for c in ("DC-2", "DC-9") for q in ("Q1", "Q2", "Q3", "Q4")}
assert got == expected, f"toc pair mismatch: missing={expected - got} extra={got - expected}"

# 3. Hash-to-file uniqueness — no two source files collide on file_hash.
collisions = spark.sql(  # type: ignore[name-defined]
    """
    SELECT file_hash, COUNT(DISTINCT source_path) AS n
    FROM audit_dev.bronze.workpapers_raw
    GROUP BY file_hash HAVING n > 1
    """
).count()
assert collisions == 0, f"{collisions} hash collisions across distinct source paths"

print("✓ lineage verification passed")
