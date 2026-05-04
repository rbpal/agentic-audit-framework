# Bronze tables (step_03_task_06).
#
# Raw landing — exact mirror of what came off ingest. Append-only;
# loose schema enforcement (most columns are STRING). Source-of-truth
# for re-derivation if downstream tiers get corrupted.
#
# Two tables + one volume per the design in step_03_task_01:
#   - workpapers_raw     (table)  — ingested xlsx workpaper rows
#   - tocs_raw           (table)  — ingested engagement TOC JSON files
#   - raw_pdfs           (volume) — original PDF workpaper files

resource "databricks_sql_table" "bronze_workpapers_raw" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["bronze"].name
  name               = "workpapers_raw"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Raw rows ingested from xlsx workpaper files. One row per (file, sheet, row). Loose schema — raw_data is a string-typed map of column-name → value. Source of truth for silver-tier derivation. step_03_task_06."

  column {
    # NOTE: this column is RESERVED FOR FUTURE USE and is currently always
    # NULL. The original "Auto-incremented per ingestion event" comment was
    # aspirational — the column is NOT GENERATED ALWAYS AS IDENTITY (Delta
    # therefore does not auto-fill) and src/agentic_audit/ingest/ never
    # populates it. Lineage and dedup downstream go via (source_path,
    # source_file_hash, sheet_name, row_index), not this column.
    name     = "ingestion_id"
    type     = "bigint"
    nullable = true
    comment  = "RESERVED FOR FUTURE USE — currently always NULL (no auto-fill, ingest does not populate). Lineage uses (source_path, source_file_hash)."
  }
  column {
    name    = "source_path"
    type    = "string"
    comment = "Full ADLS path to the xlsx file ingested (abfss://… form)."
  }
  column {
    name    = "file_hash"
    type    = "string"
    comment = "SHA-256 hex digest of the source file. Used for idempotent re-ingest detection."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier extracted from the file path / metadata."
  }
  column {
    name    = "sheet_name"
    type    = "string"
    comment = "Excel sheet name within the workbook."
  }
  column {
    name    = "row_index"
    type    = "int"
    comment = "1-based row index within the sheet (header row excluded)."
  }
  column {
    name     = "raw_data"
    type     = "map<string,string>"
    comment  = "Column name → value, all stringified. Type coercion happens at silver tier."
    nullable = false
  }
  column {
    name    = "ingested_at"
    type    = "timestamp"
    comment = "UTC timestamp at which this row landed in bronze."
  }
  column {
    name    = "ingested_by"
    type    = "string"
    comment = "Identity of the principal that performed the ingest (job MSI or operator UPN)."
  }
}

resource "databricks_sql_table" "bronze_tocs_raw" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["bronze"].name
  name               = "tocs_raw"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Raw engagement TOC JSON files (gold answers per quarter). One row per (file). raw_json holds the entire payload as a string for downstream parsing in silver. step_03_task_06."

  column {
    # NOTE: this column is RESERVED FOR FUTURE USE and is currently always
    # NULL. The original "Auto-incremented per ingestion event" comment was
    # aspirational — the column is NOT GENERATED ALWAYS AS IDENTITY (Delta
    # therefore does not auto-fill) and src/agentic_audit/ingest/ never
    # populates it. Lineage and dedup downstream go via (source_path,
    # source_file_hash, sheet_name, row_index), not this column.
    name     = "ingestion_id"
    type     = "bigint"
    nullable = true
    comment  = "RESERVED FOR FUTURE USE — currently always NULL (no auto-fill, ingest does not populate). Lineage uses (source_path, source_file_hash)."
  }
  column {
    name    = "source_path"
    type    = "string"
    comment = "Full ADLS path to the JSON file ingested."
  }
  column {
    name    = "file_hash"
    type    = "string"
    comment = "SHA-256 hex digest of the source file."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier (extracted from filename or contents)."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "Control identifier (DC-2, DC-9, …) extracted from filename."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Quarter identifier (Q1 / Q2 / Q3 / Q4)."
  }
  column {
    name     = "raw_json"
    type     = "string"
    comment  = "Verbatim JSON contents of the TOC file."
    nullable = false
  }
  column {
    name    = "ingested_at"
    type    = "timestamp"
    comment = "UTC timestamp at which this row landed in bronze."
  }
  column {
    name    = "ingested_by"
    type    = "string"
    comment = "Identity of the principal that performed the ingest."
  }
}

resource "databricks_volume" "bronze_raw_pdfs" {
  catalog_name = databricks_catalog.this.name
  schema_name  = databricks_schema.this["bronze"].name
  name         = "raw_pdfs"
  volume_type  = "MANAGED"

  comment = "Workpaper PDF originals. Read by the agent loop's evidence-retrieval step via /Volumes/audit_dev/bronze/raw_pdfs/<file>. step_03_task_06."
}
