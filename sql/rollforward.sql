-- rollforward.sql — the control a reviewer asks for first.
--
--   open at start + raised − cleared − written off = open at end
--
-- If that does not balance, an item was double-counted or dropped somewhere
-- between two months, and every figure built on the ledger is suspect. The
-- view computes the closing figure BOTH ways — by arithmetic and by counting
-- the items directly — and flags any period where they disagree, so the
-- control cannot be satisfied by the same mistake twice.
--
-- An item is open at the END of period P when it was raised on or before P and
-- has not been resolved by then. "Resolved" means cleared by a later period's
-- transactions (pass 0) or written off by decision.

CREATE VIEW IF NOT EXISTS v_rollforward AS
SELECT
    p.period_id,

    -- open at the start of P = open at the end of the period before it
    SUM(i.origin_period_id <  p.period_id
        AND (i.resolved_period_id IS NULL OR i.resolved_period_id >= p.period_id))
        AS opening_count,
    COALESCE(SUM(CASE WHEN i.origin_period_id <  p.period_id
                       AND (i.resolved_period_id IS NULL
                            OR i.resolved_period_id >= p.period_id)
                      THEN i.amount_cents END), 0)                AS opening_cents,

    SUM(i.origin_period_id = p.period_id)                         AS raised_count,
    COALESCE(SUM(CASE WHEN i.origin_period_id = p.period_id
                      THEN i.amount_cents END), 0)                AS raised_cents,

    SUM(i.resolved_period_id = p.period_id AND i.status = 'CLEARED')
        AS cleared_count,
    COALESCE(SUM(CASE WHEN i.resolved_period_id = p.period_id AND i.status = 'CLEARED'
                      THEN i.amount_cents END), 0)                AS cleared_cents,

    SUM(i.resolved_period_id = p.period_id AND i.status = 'WRITTEN_OFF')
        AS written_off_count,
    COALESCE(SUM(CASE WHEN i.resolved_period_id = p.period_id AND i.status = 'WRITTEN_OFF'
                      THEN i.amount_cents END), 0)                AS written_off_cents,

    -- closing, counted directly rather than derived
    SUM(i.origin_period_id <= p.period_id
        AND (i.resolved_period_id IS NULL OR i.resolved_period_id > p.period_id))
        AS closing_count,
    COALESCE(SUM(CASE WHEN i.origin_period_id <= p.period_id
                       AND (i.resolved_period_id IS NULL
                            OR i.resolved_period_id > p.period_id)
                      THEN i.amount_cents END), 0)                AS closing_cents

FROM recon_period p
LEFT JOIN reconciling_item i ON 1 = 1
GROUP BY p.period_id;


-- The same view with the arithmetic done and checked. The status column
-- compares the DERIVED columns rather than repeating their formula: one
-- expression, checked once, so a change to the arithmetic cannot pass a check
-- that quietly computes it a second, different way.
CREATE VIEW IF NOT EXISTS v_rollforward_check AS
SELECT d.*,
       CASE WHEN d.derived_count = d.closing_count
             AND d.derived_cents = d.closing_cents
            THEN 'BALANCED' ELSE 'BREAK' END AS status
FROM (
    SELECT r.*,
           opening_count + raised_count - cleared_count - written_off_count AS derived_count,
           opening_cents + raised_cents - cleared_cents - written_off_cents AS derived_cents
    FROM v_rollforward r
) d;