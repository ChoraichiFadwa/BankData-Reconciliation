-- aging.sql — how long each open item has been open, and what that means.
--
-- Age alone is a number; the value is in the thresholds attached to it. A
-- deposit in transit at 3 days is routine and the same item at 80 days means
-- the money never arrived. An outstanding check is normal for weeks and
-- stale-dated after six months.
--
-- Ages are measured from the ORIGIN transaction's date to the period end being
-- reported, so the same item ages by roughly 30 days each month it survives.
--
-- Parameters: :period_id, :follow_up_days, :escalate_days, :stale_days.

WITH as_of AS (
    SELECT period_end FROM recon_period WHERE period_id = :period_id
),
open_item AS (
    SELECT i.item_id,
           i.origin_period_id,
           i.category,
           i.side,
           i.amount_cents,
           i.escalated,
           COALESCE(b.txn_date, g.txn_date, p.period_end)      AS origin_date,
           COALESCE(b.description, g.memo, i.category)         AS description,
           COALESCE(b.reference, g.doc_no, '')                 AS doc_ref,
           CAST(julianday((SELECT period_end FROM as_of))
                - julianday(COALESCE(b.txn_date, g.txn_date, p.period_end)) AS INTEGER)
                                                               AS age_days
    FROM reconciling_item i
    JOIN recon_period p ON p.period_id = i.origin_period_id
    LEFT JOIN bank_txn b ON b.bank_txn_id = i.bank_txn_id
    LEFT JOIN gl_entry g ON g.gl_entry_id = i.gl_entry_id
    -- open as at the end of the reported period
    WHERE i.origin_period_id <= :period_id
      AND (i.resolved_period_id IS NULL OR i.resolved_period_id > :period_id)
)
SELECT o.*,
       CASE WHEN o.age_days <=  30 THEN '0-30'
            WHEN o.age_days <=  60 THEN '31-60'
            WHEN o.age_days <=  90 THEN '61-90'
            ELSE '90+' END                                     AS bucket,
       CASE
            -- a check that can no longer be presented at the bank
            WHEN o.category = 'OS_CHECK' AND o.age_days > :stale_days     THEN 'STALE'
            WHEN o.category = 'OS_CHECK' AND o.age_days > :follow_up_days THEN 'FOLLOW_UP'
            -- money nobody can explain, or money the bank never saw: the
            -- longer these sit, the more likely they are an error or a fraud
            WHEN o.category IN ('UNID_CREDIT', 'UNID_DEBIT',
                                'DEP_NOT_CREDITED', 'PMT_NOT_DEBITED')
                 AND o.age_days > :escalate_days                          THEN 'ESCALATE'
            -- the rest are routine timing items: they should clear next month
            WHEN o.category <> 'RESIDUAL' AND o.age_days > :follow_up_days
                                                                          THEN 'FOLLOW_UP'
            ELSE 'NONE' END                                    AS flag
FROM open_item o
ORDER BY o.age_days DESC, ABS(o.amount_cents) DESC;