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

## `@pytest.mark.slow` integration tests need a documented workflow

**Discovered:** step_05_task_02 cloud step. Both hot-fixes (PR `#57`,
PR `#58`) were in code paths that had only ever been mocked. The
integration tests that would have caught both
([`tests/integration/test_layer1_e2e.py`](../tests/integration/test_layer1_e2e.py),
[`tests/integration/test_layer2_silver_reader_e2e.py`](../tests/integration/test_layer2_silver_reader_e2e.py))
are `@pytest.mark.slow` and skipped in CI.

**What's wrong:** "Marked slow, never run live" is a class of bug, not
an instance. Any PR that touches a code path running against live
Databricks can introduce a similar bug today, and there's no automated
gate.

**Why we left it:** Adding a CI lane with warehouse credentials is
non-trivial (secret management, cost-per-run, warehouse cold-start
latency in CI) and out of scope for step_05.

**What to do when we come back:**

Two paths, pick based on team preference:

| Option | What | Trade-off |
|---|---|---|
| **A** PR-author-runs flow | Add a CONTRIBUTING.md item: "before merging any PR that touches Layer 1 / Layer 2 code, run `make integration-test-warehouse` locally". Plus a Make target that wraps the env-var dance (`DATABRICKS_HOST` / `DATABRICKS_TOKEN` / `DATABRICKS_SQL_WAREHOUSE_ID`). | Honor-system; humans forget. |
| **B** CI lane with creds | Add a GitHub Actions job that runs `pytest -m slow` against the dev warehouse. Gated to the `main` branch and to PRs from trusted contributors only (to avoid public-fork PR token leak). | Real automation; needs secret management + cost monitoring. |

Recommend **A first, B when team grows past 1 person.**

---

## Databricks PAT creation flow needs documentation

**Discovered:** step_05_task_02 cloud step. The "BI Tools scope" vs
"Other APIs" choice in the Databricks PAT generation UI is non-obvious
— "BI Tools" is the right choice for SQL-warehouse access (which is
what `scripts/run_layer1.py` and the integration tests use), but the
default-looking option is "Other APIs" with a manual scope picker.

**What's wrong:** New contributor running an integration test for the
first time has to either (a) figure this out by trial and error, or
(b) ask someone.

**Why we left it:** Step_05 doesn't ship contributor onboarding. This
is a documentation task, not a code task.

**What to do when we come back:**

- Add a `CONTRIBUTING.md` section "Running integration tests against a
  live Databricks warehouse" with a step-by-step PAT creation flow:
  - Workspace → Settings → Developer → Access tokens
  - Generate new token, **scope = BI Tools** (not Other APIs)
  - Lifetime = 1 day for one-off test runs, longer for ongoing dev
  - Copy immediately — Databricks shows it once
- Add a small `scripts/setup_warehouse_env.sh` that prompts for the
  PAT via `read -s` and exports the three required env vars. Also
  echoes verification (HOST / WAREHOUSE_ID values + TOKEN length only).
- Reference both from the slow-test entry above.

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
