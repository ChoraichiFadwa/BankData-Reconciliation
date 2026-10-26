-- reporting.sql — the views Power BI reads.
--
-- Two rules, so the dashboard can never disagree with the controls:
--   1. No accounting logic lives here. Open items, aging and flags come from
--      controls.aging() (aging.sql); the rollforward comes from
--      v_rollforward_check. These views only reshape.
--   2. One row per period uses the LATEST run of that period. Reruns are kept
--      in recon_run for audit, but a dashboard that summed them would double
--      count.

-- ---------------------------------------------------------------- latest run
CREATE VIEW IF NOT EXISTS v_latest_run AS
SELECT r.*
FROM recon_run r
WHERE r.run_id = (SELECT MAX(r2.run_id) FROM recon_run r2
                  WHERE r2.period_id = r.period_id);


-- ---------------------------------------------------------------- period KPIs
-- One row per reconciled period: match rate, the proof, and the rollforward.
--
-- Match rate = bank rows matched by passes 1-3 / bank rows in the period.
-- Pass 0 is reported separately (carried_cleared): it clears items raised in
-- EARLIER periods, so mixing it into this month's rate would flatter it.
CREATE VIEW IF NOT EXISTS v_period_kpis AS
SELECT
    p.period_id,
    p.period_end,
    p.status                                              AS period_status,
    b.bank_rows,
    r.n_exact,
    r.n_timing,
    r.n_tolerance,
    r.n_exact + r.n_timing + r.n_tolerance                AS n_matched,
    ROUND(1.0 * (r.n_exact + r.n_timing + r.n_tolerance)
          / NULLIF(b.bank_rows, 0), 4)                    AS match_rate,
    r.n_carryforward                                      AS carried_cleared,
    r.proof_diff_cents,
    CASE WHEN r.proof_diff_cents = 0 THEN 'TIES' ELSE 'BREAK' END AS proof_status,
    rf.opening_count,  rf.opening_cents,
    rf.raised_count,   rf.raised_cents,
    rf.cleared_count,  rf.cleared_cents,
    rf.written_off_count, rf.written_off_cents,
    rf.closing_count,  rf.closing_cents,
    rf.status                                             AS rollforward_status
FROM recon_period p
JOIN v_latest_run r          ON r.period_id  = p.period_id
JOIN v_rollforward_check rf  ON rf.period_id = p.period_id
JOIN (SELECT period_id, COUNT(*) AS bank_rows
      FROM bank_txn GROUP BY period_id) b ON b.period_id = p.period_id;