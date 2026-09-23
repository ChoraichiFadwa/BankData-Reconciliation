# The ledger, table by table

`sql/schema.sql` — eight tables, two views and ten triggers. This document
explains what each holds and, more importantly, which accounting rules are
enforced by the database rather than trusted to the Python.

**Conventions.** Money is `INTEGER` cents everywhere, matching the engine —
never `REAL`. Dates are ISO text (`2026-06-30`), which SQLite compares
correctly. Periods are `'YYYY-MM'` and every table hangs off one, so adding a
month is data, not a migration. Foreign keys are off by default in SQLite, so
`db.connect()` enables them per connection; without that the `REFERENCES`
clauses would be documentation rather than rules.

---

## recon_period

One row per month. `period_start`, `period_end`, four balances
(`bank_open_cents`, `bank_close_cents`, `gl_open_cents`, `gl_close_cents`),
`status` (`OPEN` / `CLOSED`) and `closed_at`.

The balances are the **stated** figures — the bank statement header and the GL
trial balance — never derived from the transactions. That is what makes the
proof capable of failing.

Bank and GL are stored separately because from the second period on they differ:
June's outstanding items mean July opens $33,133.92 apart.

*Constraints:* `period_id` must look like a period; `period_end >= period_start`;
and `(status = 'CLOSED') = (closed_at IS NOT NULL)` — a period cannot be closed
without a timestamp, or carry one while open.

## bank_txn / gl_entry

The source rows, verbatim: the date, the text, the signed amount in cents, the
extracted check number (`''` when the reference isn't one), and provenance —
`source_file`, `source_row` and `row_hash`.

*Constraint:* `UNIQUE (period_id, source_file, source_row)`. The same line of the
same file is the same transaction, so reloading is a no-op.

**Why the line number is part of a row's identity:** hashing content alone would
treat the planted duplicate posting — identical to another GL line in date, memo,
document and amount — as a re-load and silently drop it. That is the exact break
the engine exists to catch.

## recon_run

One row per execution: `run_at`, `engine_version`, the thresholds as
`params_json`, the counts per pass (`n_carryforward`, `n_exact`, `n_timing`,
`n_tolerance`), `n_raised` (items this period raised) and `n_exceptions` (items
on the rec at period end — raised plus still open from before), the proof
difference and both completeness figures.

A period can be re-run while open; the new run supersedes the previous one.
`run_id` is `AUTOINCREMENT`, so a superseded number is never handed out again
and "run 3" means one thing forever.

`n_raised` is also the rollforward's outside witness: it is what the run counted
at the time, and it cannot legitimately change afterwards.

## match

Every pair the engine made: the `pass`, the `method` (`amount + date`,
`check no.`, `date window`, `fuzzy`), both transaction ids, the days delta, the
cents delta and the fuzzy score.

*Constraints:* `UNIQUE (run_id, bank_txn_id)` and `UNIQUE (run_id, gl_entry_id)`.
**This is the one-to-one matching rule, enforced by the database.** It is what
forces a duplicate GL posting out into the exception report, and it holds even
if the matching code has a bug.

Carry-forward resolutions are *not* rows here — see `reconciling_item` below.

## reconciling_item

An exception as a living object: where it came from (`origin_period_id`,
`origin_run_id`, and the source row on one side), what it is (`category`,
`amount_cents`, `side`), where it stands (`status`, `escalated`) and how it ended
(`resolved_period_id`, `resolved_run_id`, and `cleared_bank_txn_id` or
`cleared_gl_entry_id`).

`side` is `BANK`, `GL` or `RESIDUAL` — the last for cents left over across many
matched pairs, which belong to neither file.

*Constraints:*
- an item belongs to exactly one source row, on the side it claims — except a
  `RESIDUAL`, which has none;
- `(status = 'OPEN') = (resolved_period_id IS NULL)`. **This is what keeps the
  rollforward honest:** an item cannot be marked resolved without recording where
  it was resolved, and cannot claim to be open while pointing at a resolution;
- at most one clearing transaction;
- partial unique indexes stop one source row producing two items in the same run.

## item_event

Append-only history: `CREATED`, `CLEARED`, `WRITTEN_OFF`, `ESCALATED`, `NOTE`,
each with a timestamp, the period it happened in and a note. A change of mind is
a new event, never an edit — enforced by a trigger that rejects every `UPDATE`.

## period_event

`CLOSED` and `REOPENED`, with notes. Reopening closed books is legitimate and
must never be silent, so `controls.reopen_period()` requires a reason and records
it here.

---

## The views

**`v_rollforward`** — per period: opening, raised, cleared, written off and
closing, in both counts and cents. Closing is counted directly rather than
derived.

**`v_rollforward_check`** — the same with the arithmetic done, and a
`BALANCED` / `BREAK` status comparing the derived columns to the counted ones.
The status compares the derived columns rather than repeating their formula, so
a change to the arithmetic cannot slip past a check that quietly computes it a
second, different way.

`sql/aging.sql` is a parameterized query rather than a view, because ages and
thresholds are relative to a chosen period end.

## The triggers

Ten, all enforcing one rule: **closed books stay closed.** A closed period
rejects new transactions, deleted transactions, new items, restated items
(changing an item's amount, category, side or run), deleted items and deleted
runs. `item_event` rejects updates in every period.

The important exception: a closed period's items may still be **resolved**. June
closes at the start of July, and June's outstanding check clears on July 20 —
so a status change on a closed period's item is allowed, while changing what the
item *is* is not.

These matter because they hold against anything touching the database: a future
version of this code, a stray script, or someone at a SQL prompt.

---

## A worked example: one item's life

June's Ditch Witch check, $7,412.66:

1. June's run finds no bank row for it. It is classified `OS_CHECK` and written
   as a `reconciling_item` with `status = 'OPEN'`, plus a `CREATED` event.
2. It appears on June's rec, on the bank side of the proof as an outstanding
   check, and in June's closing rollforward figure.
3. July's pass 0 finds a July bank row with the same amount and check number
   1079. The item becomes `CLEARED` with `resolved_period_id = '2026-07'`,
   `cleared_bank_txn_id` pointing at that row, and a `CLEARED` event recording
   that it matched on check number.
4. That bank row is withheld from July's own matching — it belongs to June's
   item — but still counts in July's completeness control.
5. July's rollforward shows it under `cleared`, and July's closing figure is one
   lower.

Had it not cleared — like the Vermeer check — it would still be open at the end
of August at 63 days, in the `61-90` bucket, flagged `FOLLOW_UP`, and carried on
August's rec as a bank-side reconciling item.