# Silver tables (step_03_task_07).
#
# Cleaned, conformed, deduplicated. Domain entities emerge here:
#   - controls               (the control catalog: DC-1, DC-2, …)
#   - attributes             (per-control attributes: DC-2.A, DC-2.B, …)
#   - evidence               (extracted evidence per control / attribute / quarter)
#   - cross_file_validations (cross-file consistency checks like the Q3 DC-9 figure mismatch)
#
# All MANAGED Delta tables in audit_dev.silver, with strict schema +
# real types. PII-bearing columns get masks attached in a follow-up
# step (column-mask wiring lives in step_03_task_05+ governance work).

resource "databricks_sql_table" "silver_controls" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["silver"].name
  name               = "controls"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Control catalog. One row per (control_id). Adding DC-8 next quarter is a row insert here, not a schema change. step_03_task_07."

  column {
    name    = "control_id"
    type    = "string"
    comment = "Control identifier (e.g., 'DC-2'). Conventionally formatted; populated via silver.normalize_control_id() — see step_03_task_05 design."
  }
  column {
    name    = "control_name"
    type    = "string"
    comment = "Human-readable control name."
  }
  column {
    name    = "framework"
    type    = "string"
    comment = "Compliance framework (e.g., 'SOX-IT', 'SOC2', 'PCI-DSS'). Multiple frameworks can map to the same control_id with different attribute lists."
  }
  column {
    name    = "frequency"
    type    = "string"
    comment = "Test frequency: 'Q' (quarterly), 'M' (monthly), 'A' (annual), 'OD' (on demand)."
  }
  column {
    name    = "description"
    type    = "string"
    comment = "Free-text description of what the control covers."
  }
  column {
    name    = "owner_team"
    type    = "string"
    comment = "Team or AAD group accountable for executing this control."
  }
  column {
    name    = "created_at"
    type    = "timestamp"
    comment = "When this control row was created in the silver catalog."
  }
  column {
    name    = "updated_at"
    type    = "timestamp"
    comment = "Last update timestamp (description, owner, frequency changes propagate here)."
  }
}

resource "databricks_sql_table" "silver_attributes" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["silver"].name
  name               = "attributes"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Per-control test attributes. Each control has multiple attributes (DC-2.A, DC-2.B, etc.) that the agent / human auditor evaluates separately. step_03_task_07."

  column {
    name    = "attribute_id"
    type    = "string"
    comment = "Full attribute identifier (e.g., 'DC-2.A')."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "Foreign key to silver.controls.control_id."
  }
  column {
    name    = "attribute_letter"
    type    = "string"
    comment = "Attribute letter within the control (e.g., 'A', 'B'). Useful for ordering."
  }
  column {
    name    = "description"
    type    = "string"
    comment = "What this attribute evaluates."
  }
  column {
    name    = "expected_pass_rate"
    type    = "double"
    comment = "Threshold pass rate expected for this attribute (0.0–1.0). Below this triggers an audit finding in gold.audit_findings."
  }
  column {
    name    = "created_at"
    type    = "timestamp"
    comment = "When this attribute was added."
  }
}

resource "databricks_sql_table" "silver_evidence" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["silver"].name
  name               = "evidence"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Extracted evidence per (control, attribute, quarter). Derived from bronze.workpapers_raw via the bronze→silver ETL. Every gold.agent_claims row's source_evidence_ids array references rows in this table. step_03_task_07."

  column {
    name    = "evidence_id"
    type    = "bigint"
    comment = "Primary key. Auto-generated."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier — scopes evidence to a specific audit engagement."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "Foreign key to silver.controls.control_id."
  }
  column {
    name     = "attribute_id"
    type     = "string"
    nullable = true
    comment  = "Optional foreign key to silver.attributes.attribute_id. NULL when evidence is control-level rather than attribute-level."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Quarter identifier (Q1 / Q2 / Q3 / Q4) — partition column."
  }
  column {
    name    = "source_path"
    type    = "string"
    comment = "ADLS path of the source workpaper file (traces back to bronze.workpapers_raw)."
  }
  column {
    name    = "source_file_hash"
    type    = "string"
    comment = "SHA-256 of the source file. Lineage-cross-check vs bronze."
  }
  column {
    name    = "evidence_type"
    type    = "string"
    comment = "Categorisation: 'workpaper-row', 'screenshot', 'log-entry', 'config-export', etc."
  }
  column {
    name    = "narrative"
    type    = "string"
    comment = "Free-text evidence narrative. The agent loop reads this. PII column-mask attached in governance follow-up."
  }
  column {
    name    = "ingested_at"
    type    = "timestamp"
    comment = "Bronze→silver ETL timestamp."
  }
}

resource "databricks_sql_table" "silver_cross_file_validations" {
  catalog_name       = databricks_catalog.this.name
  schema_name        = databricks_schema.this["silver"].name
  name               = "cross_file_validations"
  table_type         = "MANAGED"
  data_source_format = "DELTA"

  comment = "Cross-file consistency checks (e.g., the Q3 DC-9 figure mismatch from step_01_synthetic_data). Each row records a comparison between values in TOC vs workpaper for a given (control, quarter). step_03_task_07."

  column {
    name    = "validation_id"
    type    = "bigint"
    comment = "Primary key."
  }
  column {
    name    = "engagement_id"
    type    = "string"
    comment = "Engagement identifier."
  }
  column {
    name    = "control_id"
    type    = "string"
    comment = "Foreign key to silver.controls.control_id."
  }
  column {
    name    = "quarter"
    type    = "string"
    comment = "Quarter identifier."
  }
  column {
    name    = "validation_type"
    type    = "string"
    comment = "Type of cross-file check: 'figure_mismatch', 'date_mismatch', 'missing_evidence', 'duplicate_attribution', etc."
  }
  column {
    name    = "description"
    type    = "string"
    comment = "Free-text description of what was checked + the discrepancy found."
  }
  column {
    name     = "toc_value"
    type     = "string"
    nullable = true
    comment  = "Value from the engagement TOC (gold answer)."
  }
  column {
    name     = "workpaper_value"
    type     = "string"
    nullable = true
    comment  = "Value from the workpaper file."
  }
  column {
    name    = "status"
    type    = "string"
    comment = "'pass' (no discrepancy) or 'fail' (discrepancy detected)."
  }
  column {
    name    = "created_at"
    type    = "timestamp"
    comment = "When the validation was computed."
  }
}
