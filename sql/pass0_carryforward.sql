-- pass0_carryforward.sql — the query Pass 0 is built on.
--
-- Everything the engine's other passes need is in the two files of the month
-- being reconciled. Pass 0 is different: its input is the LEDGER. It asks what
-- is still open from earlier periods, and finds this period's transactions
-- that would clear those items.
--
-- Why this cannot be done from the files alone: a check written June 28 and
-- cleared July 20 is 22 days apart, so no timing window reaches it — and the
-- July GL has no row for it at all, because it was booked in June. Without the
-- ledger, that bank row is an unidentified debit and the check looks lost.
--
-- Three ways an item resolves:
--   1. a GL-side item (outstanding check, payment or deposit in transit)
--      clears when the BANK finally shows it;
--   2. a bank-side item (charge, interest, NSF return, unidentified) clears
--      when the bookkeeper BOOKS it — the journal entry the exception report
--      asked for;
--   3. a duplicate posting clears when it is REVERSED: same amount, opposite
--      sign, on the same side; and the pass-3 residual clears when its
--      write-off JE is booked.
--
-- Returns candidates, not decisions: one item can have several, and one
-- transaction can look right for several items. Python assigns them one to
-- one, strictest rule first. :period_id is the period being reconciled.

WITH open_item AS (
    SELECT i.item_id,
           i.side,
           i.category,
           i.amount_cents,
           i.origin_period_id,
           COALESCE(b.check_no, g.check_no, '') AS check_no,
           COALESCE(b.txn_date, g.txn_date)     AS origin_date
    FROM reconciling_item i
    LEFT JOIN bank_txn b ON b.bank_txn_id = i.bank_txn_id
    LEFT JOIN gl_entry g ON g.gl_entry_id = i.gl_entry_id
    -- open as of the START of this period: items still open, plus items a
    -- PREVIOUS run of this same period cleared (a re-run takes those back,
    -- so they are open again from this run's point of view)
    WHERE (i.status = 'OPEN' OR i.resolved_period_id = :period_id)
      AND i.origin_period_id < :period_id          -- raised in an earlier period
)

-- 1. the bank finally shows a GL-side item
SELECT o.item_id, o.category, o.amount_cents, o.origin_period_id, o.origin_date,
       'BANK'          AS clearing_side,
       b.bank_txn_id   AS clearing_id,
       b.txn_date      AS clearing_date,
       b.description   AS clearing_text,
       CASE WHEN o.check_no <> '' THEN 'check no.' ELSE 'amount' END AS rule
FROM open_item o
JOIN bank_txn b
  ON b.period_id = :period_id
 AND b.amount_cents = o.amount_cents
 AND (o.check_no = '' OR b.check_no = o.check_no)
WHERE o.side = 'GL' AND o.category <> 'DUPLICATE'

UNION ALL

-- 2. the bookkeeper posts a bank-side item
SELECT o.item_id, o.category, o.amount_cents, o.origin_period_id, o.origin_date,
       'GL', g.gl_entry_id, g.txn_date, g.memo, 'booked in GL'
FROM open_item o
JOIN gl_entry g
  ON g.period_id = :period_id
 AND g.amount_cents = o.amount_cents
WHERE o.side = 'BANK'

UNION ALL

-- 2b. the pass-3 residual is written off
SELECT o.item_id, o.category, o.amount_cents, o.origin_period_id, o.origin_date,
       'GL', g.gl_entry_id, g.txn_date, g.memo, 'residual write-off'
FROM open_item o
JOIN gl_entry g
  ON g.period_id = :period_id
 AND g.amount_cents = o.amount_cents
WHERE o.category = 'RESIDUAL'

UNION ALL

-- 3. a duplicate posting is reversed
SELECT o.item_id, o.category, o.amount_cents, o.origin_period_id, o.origin_date,
       'GL', g.gl_entry_id, g.txn_date, g.memo, 'reversal'
FROM open_item o
JOIN gl_entry g
  ON g.period_id = :period_id
 AND g.amount_cents = -o.amount_cents
WHERE o.category = 'DUPLICATE'

ORDER BY item_id, clearing_date;