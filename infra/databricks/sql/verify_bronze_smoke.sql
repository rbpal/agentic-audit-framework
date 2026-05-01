-- step_03_task_09 — bronze smoke ingest verification
--
-- Run from a SQL warehouse that has SELECT on audit_dev.bronze.*. Each query
-- has an inline assertion comment describing the expected result for a clean
-- Step-1 corpus ingest. Failures indicate either a bad ingest run or a corpus
-- drift that needs investigation.

-- 1. Row counts per bronze table.
--    Expected: workpapers_raw ≥ 100 (sum of non-empty rows across 8 xlsx);
--              tocs_raw = 8.
SELECT 'workpapers_raw' AS tbl, COUNT(*) AS rowcount FROM audit_dev.bronze.workpapers_raw
UNION ALL
SELECT 'tocs_raw'       AS tbl, COUNT(*) AS rowcount FROM audit_dev.bronze.tocs_raw;

-- 2. Distinct file_hash count.
--    Expected: 8 in each table (DC-2/DC-9 × Q1-Q4 × {workpaper, toc}).
SELECT 'workpapers_raw' AS tbl, COUNT(DISTINCT file_hash) AS distinct_hashes
FROM audit_dev.bronze.workpapers_raw
UNION ALL
SELECT 'tocs_raw'       AS tbl, COUNT(DISTINCT file_hash) AS distinct_hashes
FROM audit_dev.bronze.tocs_raw;

-- 3. (control_id, quarter) coverage in tocs_raw.
--    Expected: all 8 (DC-{2,9} × Q{1..4}) present, no duplicates.
SELECT control_id, quarter, COUNT(*) AS n
FROM audit_dev.bronze.tocs_raw
GROUP BY control_id, quarter
ORDER BY control_id, quarter;

-- 4. Hash collision check — no two distinct source paths share a file_hash.
--    Expected: zero rows returned.
SELECT file_hash, COUNT(DISTINCT source_path) AS distinct_sources
FROM audit_dev.bronze.workpapers_raw
GROUP BY file_hash
HAVING distinct_sources > 1;

-- 5. Per-file row breakdown — handy for eyeballing post-ingest.
SELECT
    source_path,
    sheet_name,
    COUNT(*) AS rows_ingested,
    MIN(ingested_at) AS ingested_at
FROM audit_dev.bronze.workpapers_raw
GROUP BY source_path, sheet_name
ORDER BY source_path, sheet_name;

-- 6. Re-run idempotency check.
--    Run the smoke notebook twice in a row, then confirm:
--      - workpapers_raw row count is unchanged between runs
--      - DESCRIBE HISTORY shows two MERGE operations, second with
--        numTargetRowsInserted = 0.
DESCRIBE HISTORY audit_dev.bronze.workpapers_raw;
DESCRIBE HISTORY audit_dev.bronze.tocs_raw;
