# Databricks / Unity Catalog design

This directory will hold the Databricks-specific Terraform modules
and configuration that build on Step 2's substrate (workspace,
Access Connector, ADLS Gen2 with bronze/silver/gold filesystems).

The catalog / schema / table topology is designed in
`step_03_task_01` and provisioned across `step_03_task_02` through
`step_03_task_09`.

---

## Design at a glance

```
metastore (eastus2)               ← step_03_task_02 provisions
└── audit_dev                     ← step_03_task_05 provisions
    ├── bronze                    ← raw landing; ingest writes here
    │   ├── workpapers_raw        (table)
    │   ├── tocs_raw              (table)
    │   └── raw_pdfs              (volume)
    ├── silver                    ← cleaned / conformed / typed
    │   ├── controls              (table)
    │   ├── attributes            (table)
    │   ├── evidence              (table)
    │   ├── cross_file_validations(table)
    │   ├── normalize_control_id  (function)
    │   ├── parse_quarter         (function)
    │   └── redact_pii            (function — defensive PII masking)
    └── gold                      ← curated; agent + eval consume
        ├── agent_claims          (table)
        ├── audit_findings        (table)
        ├── eval_outcomes         (table)
        ├── cost_telemetry        (table)
        ├── control_summary       (view — by quarter)
        ├── control_extractor     (model)
        └── evidence_embedding    (model)
```

Same structure replicates as `audit_prod` when the prod environment
is created — sibling catalog, identical schemas, identical naming.

---

## Key design decisions

### Catalog-per-environment (not env-prefixed schemas)

`audit_dev` and (later) `audit_prod` are separate Unity Catalog
catalogs, not schemas inside a shared `audit` catalog. The catalog
is the natural permission boundary: `GRANT ... ON CATALOG audit_prod
TO <auditors>` is one statement that protects every prod table —
including tables added in the future. The env-prefixed-schema
alternative requires a grant per schema and silently leaks any
schema added later that's missed in the grant list.

### Schema-per-medallion-tier (not schema-per-domain)

Inside each catalog, three schemas — `bronze`, `silver`, `gold` —
mirror the medallion architecture and the ADLS filesystem layout from
`step_02_task_04` one-to-one:

- **bronze** — raw landing; append-only; loose schema; cold-tier
  storage; 7-year retention (SOX 802).
- **silver** — cleaned, conformed, typed; idempotent MERGE from
  bronze; strict schema; PII column masks applied here.
- **gold** — curated for direct consumption by the agent loop, eval
  harness, dashboards, partner-review reports.

Schemas partition by *data quality / lifecycle*, not by *domain*.
Domain (controls vs evidence vs claims) is encoded at the table /
volume level inside each schema.

### Lineage as a permissions invariant

Data flows bronze → silver → gold one-way. Enforced in the permission
matrix: no principal has `MODIFY` on two tiers in the backwards
direction. Any backwards write violates the matrix, which makes the
SOX audit *"did anyone write to bronze who shouldn't have?"* answerable
from `system.access.audit` with one filter.

### Storage credential via the Access Connector MSI

Unity Catalog reads / writes ADLS through the Databricks Access
Connector MSI (`dbw-aaf-dev-connector`) provisioned in
`step_02_task_05`. `step_03_task_03` grants this MSI
`Storage Blob Data Contributor` on `dlsaafrbpaldev`;
`step_03_task_04` defines the `storage_credential` and
`external_location`s that bind UC catalog/schema names to ADLS
paths.

### MSI-first auth across all governed objects

Every non-human principal in the permission matrix (ingest job, ETL
jobs, agent loop, eval harness) authenticates via Azure AD managed
identity — no API keys at runtime. Carries forward the
*"no API keys at runtime"* stance from Step 2 §3.5.

---

## Public-vs-private documentation

This README is the **public-facing** summary — enough for any repo
reviewer to understand the design at a glance and judge whether
subsequent Terraform / SQL changes match it.

The deep walkthrough — pedagogical chunks on Unity Catalog
fundamentals, alternative-rejection trade-offs, defensibility
pitches, interview Q&A drills, Federated Hermes scaling — lives in
`privateDocs/step_03_unity_catalog.md` (gitignored), authored as
study material rather than reference documentation.

---

## Task index

| Task | Scope |
|---|---|
| `step_03_task_01` | Design catalog and schema hierarchy (this README) |
| `step_03_task_02` | Provision Unity Catalog metastore (Terraform; account-level) |
| `step_03_task_03` | Bind workspace to metastore + grant Access Connector MSI on ADLS |
| `step_03_task_04` | Storage credential + external locations (bronze / silver / gold) |
| `step_03_task_05` | Create catalog + schemas via Terraform `databricks` provider |
| `step_03_task_06` | Define bronze tables (raw ingest landing) |
| `step_03_task_07` | Define silver tables (cleaned + conformed) |
| `step_03_task_08` | Define gold tables (curated for agent + eval) |
| `step_03_task_09` | Smoke ingest from Step 1 corpus → bronze; verify lineage |
