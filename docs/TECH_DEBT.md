# Technical debt log

Open items that the codebase is *aware of* but has chosen not to fix
in the task they surfaced in. Each entry says **what's wrong**,
**why we left it**, and **what to do when we come back**.

This file is the central register so future tasks don't have to
re-discover the same issues. When opening a debt-paying PR, link the
PR back to the entry here and remove it from this file.

---

## bronze.{workpapers_raw, tocs_raw}.ingestion_id

**Discovered:** step_05_task_02 cloud step (2026-05-03). See
[`privateDocs/step_05_layer2_narrative.md`](../../privateDocs/step_05_layer2_narrative.md)
> "Execution log — how task_02 actually shipped" > "Hot-fix #1 (PR #57)".

**What's wrong:**

- Column is declared in
  [`infra/terraform/modules/databricks_uc/tables_bronze.tf`](../infra/terraform/modules/databricks_uc/tables_bronze.tf)
  as a plain nullable `bigint`. The original comment claimed
  "Auto-incremented per ingestion event. Primary key for the row." —
  but the column is **NOT** declared `GENERATED ALWAYS AS IDENTITY`,
  so Delta never auto-fills it.
- The bronze ingest code
  ([`src/agentic_audit/ingest/bronze_smoke.py`](../src/agentic_audit/ingest/bronze_smoke.py))
  does **NOT** populate `ingestion_id` either — the `WorkpaperRow` /
  `TocRecord` dataclasses don't even have the field.
- Net effect: every existing bronze row has `ingestion_id = NULL`.
  Verified live during the step_05 cloud step.

**What it actually does:** nothing. The column is dead weight today.

- No downstream code reads `r.ingestion_id` from the parsed
  `BronzeWorkpaperRow` model.
- Silver / gold lineage uses `source_path` + `source_file_hash`, not
  `ingestion_id`.
- Silver MERGE deduplication keys on
  `(engagement_id, control_id, attribute_id, quarter)`, not
  `ingestion_id`.

**Why we left it:**

- Removing the column requires Delta column-mapping mode
  (`delta.columnMapping.mode = 'name'`) which our
  `databricks_sql_table` resources don't currently declare. Adding
  column-mapping retroactively is itself a Terraform-on-Databricks
  schema migration — and step_05_task_02 surfaced just how flaky those
  are (hot-fixes #1 and #2 + a manual SQL ALTER intervention for one
  column).
- Removing now also means re-adding later if any future task wants a
  real ingest-batch ID for "show me all rows from batch X" queries
  (audit pipelines often want this).
- A NULL column costs essentially nothing today. Cheaper to leave the
  placeholder.

**Mitigation already in place:**

- [`src/agentic_audit/layer1_extract/bronze_reader.py`](../src/agentic_audit/layer1_extract/bronze_reader.py)
  declares `BronzeWorkpaperRow.ingestion_id` as `Optional[int]`
  (PR `#57`). The pydantic model reflects what bronze actually carries.
- The Terraform comment was rewritten (this PR) to say
  *"RESERVED FOR FUTURE USE — currently always NULL"* with a pointer
  back here. No more comment lie.

**What to do when we come back:**

Two reactivation options, pick based on what use case prompts the
revisit:

| Option | When | What |
|---|---|---|
| **A** Auto-incrementing identity | We want a globally unique row ID for cross-batch debugging or external FK reference | (1) Add `delta.columnMapping.mode = 'name'` table property to both bronze tables in Terraform. (2) Drop the existing `ingestion_id` columns. (3) Re-add as `GENERATED ALWAYS AS IDENTITY`. (4) Backfill existing rows with `ROW_NUMBER() OVER (...)`. (5) Run `scripts/run_layer1.py`-like driver to repopulate any downstream consumers if needed. |
| **B** Per-batch run ID | We want "show me all rows from one ingest batch" semantics | (1) Add `ingestion_id: str` to `WorkpaperRow` + `TocRecord` dataclasses in `bronze_smoke.py`. (2) Generate a fresh ULID at the top of every `ingest_workpapers` / `ingest_tocs` invocation; pass to every row. (3) Backfill existing rows with one ULID per source-file-hash group. (4) Update `BronzeWorkpaperRow.ingestion_id` to required (was Optional). (5) Update tests. |

Option A needs a column-mapping-mode migration first (one Terraform
PR, one cloud step). Option B doesn't need any schema change but
requires a Python ingest update + backfill. Either should be a
dedicated task, not bundled into something else.

---

## ~~infra/terraform/errored.tfstate~~ — RESOLVED 2026-05-03

The original entry has been resolved by deleting the local file.
Investigation during the cleanup also re-shaped what the entry was
actually about — leaving the historical context here so future readers
don't trip on the same false-positive.

**What we initially thought** (and what the entry described): a stale
forensic snapshot of Terraform state from a prior failed apply, sitting
in the working tree.

**What we discovered while cleaning up:**

1. **The file was never tracked by git.** `.gitignore`'s `*.tfstate`
   pattern correctly excluded it. `git ls-files --error-unmatch
   infra/terraform/errored.tfstate` returned "did not match any file(s)
   known to git". So it had never been committed, never been pushed,
   never been on GitHub.
2. **It DID contain real plaintext secrets** — `dlsaafrbpaldev` storage
   account `primary_access_key` + `secondary_access_key` + derived
   connection strings + App Insights `instrumentation_key`. The
   `.gitignore` warning *"CRITICAL: plaintext secrets live here"* was
   exactly right about the file's contents.
3. **No public exposure occurred.** The combination of #1 and #2 means
   the keys were real and live, but they only existed locally — not in
   git history, not on GitHub, not visible to forks (repo had 0 forks
   anyway).
4. **Cleanup was therefore trivial:** `rm` the local file (NOT
   `git rm` — there was nothing to git-remove). No history rewrite,
   no force-push, no key rotation needed.

**Lessons captured for future similar findings:**

1. **Always confirm git tracking before assuming public exposure.**
   `.gitignore` blocks adds; if a file matches a pattern, it's
   probably never been tracked even if it sits in the working tree.
   Run `git ls-files --error-unmatch <path>` to verify before
   escalating to "rotate keys / rewrite history".
2. **`.gitignore`'s `CRITICAL` comments work as designed.** This was
   the specific scenario the comment was put there to guard against —
   a `*.tfstate` file appearing in the working tree with secrets in
   it. The pattern caught the commit; the comment caught the
   developer's attention before the cleanup. System working.
3. **Backup-side risk is a separate concern.** A local file with
   secrets on a laptop with cloud backup (Time Machine / iCloud /
   Dropbox) could exfiltrate the keys outside git's blast radius.
   That's a contributor-side hygiene question, not a repo question.

**Action taken:**

- `rm infra/terraform/errored.tfstate` locally (this PR's commit
  message records the deletion since the file itself wasn't tracked).
- This entry retained as a resolved-with-context record so future
  contributors who see references in old PRs / docs to
  `errored.tfstate` understand what it was and why it's gone.

---

## ~~`@pytest.mark.slow` integration tests need a documented workflow~~ — RESOLVED 2026-05-03 (Path A)

**Discovered:** step_05_task_02 cloud step. Both hot-fixes (PR `#57`,
PR `#58`) were in code paths that had only ever been mocked.

**Resolution chosen: Path A — PR-author-runs flow.** Path B (CI lane
with warehouse creds) deferred until team grows past 1 person.

**Action taken:**

- Added [`scripts/setup_warehouse_env.sh`](../scripts/setup_warehouse_env.sh)
  — interactive helper that exports the three required env vars
  (`DATABRICKS_HOST`, `DATABRICKS_SQL_WAREHOUSE_ID`,
  `DATABRICKS_TOKEN`). Token read via `read -s` (silent — no terminal
  echo, no shell history). Refuses to run if executed instead of
  sourced. Verifies and prints HOST + WAREHOUSE values plus token
  length only — never the token value.
- Added Make target `make integration-test-warehouse`
  ([`Makefile`](../Makefile)). Pre-flight checks the three env vars;
  if missing, prints a clear error pointing back at the setup script
  and exits without running tests. If env is good, runs
  `pytest -m slow tests/integration/ -v`.
- Added [`CONTRIBUTING.md`](../CONTRIBUTING.md) with a section
  "Running integration tests against a live Databricks warehouse":
  - When to run them (concrete list of paths whose changes warrant
    the gate, including `layer1_extract/`, `silver_reader.py`,
    `databricks_uc/tables_*.tf`, the operator scripts, and SQL string
    construction in general).
  - Why the gate exists (links back to this entry's incident write-up).
  - How to generate a PAT in the Databricks UI (with the
    "BI Tools scope, NOT Other APIs" gotcha called out).
  - How to use the setup script + Make target.
  - Cost/cleanup notes (test runtime, scoped DELETE cleanup, PAT
    auto-expiry).

**Trigger condition for Path B revisit:** team has more than one
contributor, OR a "marked-slow-skipped-in-CI" bug reaches main again
despite the documented PR-author-runs flow.

**What Path B would look like when we come back:**

- GitHub Actions job that runs `pytest -m slow tests/integration/`
  against the dev warehouse.
- Workflow file gated to `pull_request` events on the `main` branch
  AND to PRs from trusted contributors only (to avoid public-fork PR
  token leak — public-fork PRs cannot access secrets by default in
  GitHub Actions, but verify the safeguard).
- Secret management: store the PAT as a repo-level GitHub Actions
  secret. Rotate quarterly. Use a dedicated service-principal-style
  token (separate from human dev tokens) so audit trails are clean.
- Cost monitoring: serverless warehouse cold-start adds ~2 min per
  run; warm runs are ~30–60 sec. Budget ~$0.50/PR if warehouse is
  cold; trivial if warm.

---

## ~~Databricks PAT creation flow needs documentation~~ — RESOLVED 2026-05-03

**Discovered:** step_05_task_02 cloud step. The "BI Tools scope" vs
"Other APIs" choice in the Databricks PAT generation UI is non-obvious
— "BI Tools" is the right choice for SQL-warehouse access, but the
default-looking option is "Other APIs" with a manual scope picker.

**Resolved by the same PR that resolved item #3** (slow-test workflow).
Both items had overlapping deliverables; addressing them in one PR
avoided duplicating the CONTRIBUTING.md section + the setup script.

**Action taken:**

- [`CONTRIBUTING.md`](../CONTRIBUTING.md) > "Running integration tests
  against a live Databricks warehouse" > "What you need" — step-by-step
  PAT generation walkthrough including the explicit "**Scope:** click
  **BI Tools** tab (NOT 'Other APIs' — that's the common gotcha)"
  callout, plus lifetime guidance and the "copy immediately, Databricks
  shows it once" warning.
- [`scripts/setup_warehouse_env.sh`](../scripts/setup_warehouse_env.sh)
  prompts for the PAT via `read -s` and exports the three required env
  vars. Also reminds the contributor of the BI-Tools-scope gotcha
  inline ("Scope: BI Tools (NOT 'Other APIs')") just before the token
  prompt — so the gotcha appears at the moment of decision, not just
  in static docs they may have skimmed past.

**Trigger condition for revisit:** the BI Tools / Other APIs naming
changes in the Databricks UI (unlikely but possible — Databricks
occasionally renames scope buckets). Update both files if it does.

---

## Format of new entries

Each entry should follow the template:

```markdown
## <component or feature>

**Discovered:** <which task / PR surfaced it>

**What's wrong:** <concrete description, with file links>

**Why we left it:** <honest cost/benefit rationale; not "we forgot">

**Mitigation already in place:** <if any>

**What to do when we come back:** <concrete plan, ideally with options
table if there's a real choice>
```

Resist the temptation to write entries that say "we should refactor
X someday." Specifically: only log debt that has a **clear remediation
path** and a **concrete trigger condition** for revisiting. Vague debt
becomes folklore, not an actionable register.
