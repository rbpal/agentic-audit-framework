-- Step 6 task_05 — Divergence analysis: judge vs FactChecker.
--
-- Three queries against audit_dev.gold.judge_outcomes that classify every
-- row in a single judge sweep along three axes:
--
--   1. judge_verdict   (pass / fail / uncertain)   — semantic LLM-as-judge
--   2. fact_check_verdict (pass / fail)             — deterministic FactChecker
--   3. gold_expected_verdict (pass / fail)          — ground truth from ToC
--
-- The 4-class divergence matrix (excluding uncertain) is the load-bearing
-- artifact for Step 7's gate design — semantic-only fails are exactly the
-- failure mode Step 6 exists to catch; judge-misses-grounding rows tell us
-- when to weight FactChecker over the judge.
--
-- All three queries take :judge_run_id as a bind parameter. Operator usage:
--   :judge_run_id = '4D5DFE6F036070D12DCF98F7ADCAD90F'      -- pick a sweep
--   OR use the latest-sweep CTE pattern in Q1 if you want "most recent."
--
-- See privateDocs/step_06_eval_harness.md → Working notes for the human
-- interpretation of the rows that come back.


-- ============================================================================
-- Q1 — Divergence class summary (the headline number for task_07 writeup)
-- ============================================================================
--
-- Classes (judge_verdict × fact_check_verdict, ignoring gold for this view):
--
--   concordant-pass           judge=pass    fc=pass     -- both agree, narrative is clean
--   concordant-fail           judge=fail    fc=fail     -- both agree, narrative is bad
--   semantic-only-fail        judge=fail    fc=pass     -- the bug Step 6 catches:
--                                                          numerics ground but semantics drift
--   judge-misses-grounding    judge=pass    fc=fail     -- FactChecker more conservative;
--                                                          either judge too lenient
--                                                          OR FC false positive
--   judge-uncertain           judge=uncertain           -- judge declined to call;
--                                                          report separately
--
-- For a healthy v1 baseline:
--   concordant-pass ≫ everything else
--   semantic-only-fail rows must be inspected (use Q2)
--   judge-uncertain rows must be inspected (use Q3) — and judge_status
--                                                     filtered to "ok" to
--                                                     separate genuine
--                                                     uncertain from LLM-fallback

SELECT
    CASE
        WHEN judge_verdict = 'uncertain'                        THEN 'judge-uncertain'
        WHEN judge_verdict = 'pass'  AND fact_check_verdict = 'pass' THEN 'concordant-pass'
        WHEN judge_verdict = 'fail'  AND fact_check_verdict = 'fail' THEN 'concordant-fail'
        WHEN judge_verdict = 'fail'  AND fact_check_verdict = 'pass' THEN 'semantic-only-fail'
        WHEN judge_verdict = 'pass'  AND fact_check_verdict = 'fail' THEN 'judge-misses-grounding'
    END AS divergence_class,
    COUNT(*)                          AS n_rows,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct_of_sweep
FROM   audit_dev.gold.judge_outcomes
WHERE  judge_run_id = :judge_run_id
GROUP  BY 1
ORDER  BY n_rows DESC;


-- ============================================================================
-- Q2 — Non-concordant row detail (everything that isn't both-pass / both-fail)
-- ============================================================================
--
-- Joins gold.narratives via composite key (engagement, control, quarter,
-- attribute, prompt_version) — workaround for the narrative_run_id schema
-- misnomer flagged in Step 5 follow-up #5. Once that follow-up lands and
-- gold.narratives gets a per-call narrative_call_id, this join collapses
-- to a single ON narrative_call_id = narrative_run_id condition.
--
-- Returns: scope + judge verdict + FC verdict + gold verdict + the strings
-- a human reviewer needs to settle the divergence.

WITH sweep AS (
    SELECT *
    FROM   audit_dev.gold.judge_outcomes
    WHERE  judge_run_id = :judge_run_id
)
SELECT
    j.engagement_id,
    j.control_id,
    j.quarter,
    j.attribute_id,
    j.judge_verdict,
    j.fact_check_verdict,
    j.gold_expected_verdict,
    j.judge_confidence,
    j.judge_status,
    j.cited_evidence_fields,
    j.judge_reasoning,
    n.narrative_text,
    n.fact_check_issues
FROM   sweep j
LEFT   JOIN audit_dev.gold.narratives n
    ON  n.engagement_id   = j.engagement_id
    AND n.control_id      = j.control_id
    AND n.quarter         = j.quarter
    AND n.attribute_id    = j.attribute_id
    AND n.prompt_version  = 'v1.0'
WHERE  NOT (j.judge_verdict = 'pass' AND j.fact_check_verdict = 'pass')
  AND  NOT (j.judge_verdict = 'fail' AND j.fact_check_verdict = 'fail')
ORDER  BY j.control_id, j.quarter, j.attribute_id;


-- ============================================================================
-- Q3 — Three-way agreement against gold (correctness check, not just divergence)
-- ============================================================================
--
-- Cross-tabulates (judge_verdict, fact_check_verdict) against
-- gold_expected_verdict so we can see "which combinations land where vs
-- ground truth." For a sweep with k expected-fail rows in scope:
--
--   right answers = (judge=fail OR fc=fail) on every expected-fail row
--                   AND (judge=pass AND fc=pass) on every expected-pass row
--
-- This view exposes the question: when judge and FC disagree, who is right?
-- Used in task_07 to argue the gate-composition recommendation
-- ("believe-either-fail" vs "require-both-pass" vs etc.).

SELECT
    gold_expected_verdict,
    judge_verdict,
    fact_check_verdict,
    COUNT(*) AS n_rows
FROM   audit_dev.gold.judge_outcomes
WHERE  judge_run_id = :judge_run_id
GROUP  BY 1, 2, 3
ORDER  BY 1, 2, 3;
