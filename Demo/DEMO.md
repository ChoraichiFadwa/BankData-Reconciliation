# Demo — a guided tour in eight acts

A script for showing this project to someone: what to run, what they'll see, and
what each moment is meant to prove. About 15 minutes at a comfortable pace, or 5
if you run acts 1, 3, 4 and 6 only.

**Setup, once, before anyone is watching:**

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
python src/generate_data.py
del ledger.db            # rm -f ledger.db on macOS/Linux
```

Start with no `ledger.db`. Building it live is half the story.

---

## Act 1 — The problem, in two files

> "Two records of the same bank account. They should agree. They never do."

```bash
head -3 data/bank_statement_jun2026.csv
head -3 data/gl_cash_extract_jun2026.csv
cat data/balances_jun2026.csv
```

**Point at:** the same transaction is described differently on each side
(`EFT WESCO DIST CANADA REF40241` versus `WESCO Distribution — invoice payment`),
and the dates don't always agree, because a check written on the 10th clears on
the 13th.

**The third file is the one to dwell on.** `balances_jun2026.csv` holds the
*stated* closing balances — what the bank statement header says and what the
trial balance says. Not computed from the rows. Act 3 shows why that matters.

---

## Act 2 — Reconcile one month

```bash
python src/reconcile.py --period 2026-06
```

Expect:

```
Pass 1 exact:      95
Pass 2 timing:     15  (check no. 13, date window 2)
Pass 3 tolerance:   8
Total reconciled: 118
Open items:        19  (bank-only 9, GL-only 10)
Unreconciled difference: 0.00  TIES
Saved run 1 to the ledger: 118 matches, 19 items raised, 0 cleared
```

**What to say:** three passes, strictest first. Exact clears most of the file
with no risk. Timing matches checks on check number whatever the lag, because
the bank echoes the number, and that's the only thing that pairs a check written
June 5 with one cleared June 16. Tolerance handles cents-level differences but
only when the descriptions actually agree, because amount-only matching is how
reconciliation tools invent false matches.

Open `output/reconciliation_report_jun2026.xlsx` → **Exceptions** tab.

**Point at:** every break is ranked by dollar exposure, diagnosed, and carries
the journal entry that clears it. Rank 1 is a $48K deposit in transit; the last
row is $0.35 of leftover cents. *"This is the difference between a tool that
tells you there's a problem and one that tells you what to do about it."*

---

## Act 3 — Make the proof fail

> "Any tool can print a zero. The question is whether it could ever print
> anything else."

```bash
python demo/break_proof.py
```

Expect:

```
Intact June:
  bank completeness         0.00
  unreconciled diff         0.00   TIES

Removed one bank row: 2026-06-09  PAD INTACT INS REF40211  -5170.46
Damaged June:
  bank completeness    -5,170.46   <- the bank file no longer rolls to its stated closing balance
  GL completeness           0.00   <- the GL file is fine, so the break is on the bank side
  unreconciled diff    -5,170.46   DOES NOT TIE
```

**The point:** the first version of this project computed both balances as
`opening + sum(rows)`. Every unmatched row then landed on one side or the other,
so the difference cancelled to zero *for any input whatsoever*. The control was
circular — it could not fail. Truncated exports and filtered reports are a real
failure mode, and the old proof would have said 0.00 anyway.

**If you only tell one story about this project, this is a strong candidate:**
you found a check in your own work that couldn't fail, and replaced it with one
that breaks by exactly the amount of a missing row and names the file it came
from.

---

## Act 4 — Reconcile July with no memory

> "Here's the same tool on the next month, still stateless."

```bash
python src/reconcile.py --bank data/bank_statement_jul2026.csv \
  --gl data/gl_cash_extract_jul2026.csv --balances data/balances_jul2026.csv
```

Expect **19 open items** and `Unreconciled difference: -33,133.92  DOES NOT TIE`.

**What to say, pointing at the exception list:** fourteen of these are fiction.
June's outstanding checks cleared at the bank in July, so those bank rows have no
partner in the July GL and look like unidentified debits. The bookkeeper posted
the journal entries June's report asked for, so those look like payments the bank
never debited. And $33,133.92 is not a coincidence — it's exactly the gap between
the bank's and the GL's opening balances, which is June's outstanding items.

**The lesson:** a reconciliation isn't a monthly file comparison, it's a
continuous control. June's breaks were never "results" — they were open items
with a future. A tool that forgets them can't do the job.

*(This comparison is a permanent test in the repo:
`test_july_without_pass_0_is_wrong`.)*

---

## Act 5 — The same July, with a ledger

```bash
python src/reconcile.py --period 2026-07
```

Expect:

```
Pass 0 carry-forward: cleared 15 prior items, 4 still open
...
Open items:        10  (bank-only 5, GL-only 5)
Unreconciled difference: 0.00  TIES

  period   opening  raised  cleared  w/off  closing        exposure  status
 2026-06         0      19        0      0       19       25,153.12  BALANCED
 2026-07        19       6       15      0       10       21,019.94  BALANCED
```

**What to say:** Pass 0 runs before everything else, and it's the only pass whose
input isn't a file — it asks the database what's still open. Four ways an item
resolves: a GL-side item clears when the bank finally shows it; a bank-side item
clears when the bookkeeper books it; a duplicate clears when it's reversed; a
residual clears when its write-off is booked.

Show the SQL — it reads well out loud:

```bash
head -30 sql/pass0_carryforward.sql
```

**Worth mentioning:** the SQL returns *candidates*, not decisions. One item can
have several and one transaction can suit several items, so the join is
declarative and the one-to-one assignment — check number beating a bare amount —
is Python. Each tool doing what it's good at.

Then finish the close:

```bash
python src/reconcile.py --period 2026-08
```

---

## Act 6 — What's still open, and what it means

```bash
python src/controls.py --aging 2026-08
```

Expect:

```
    0-30 days:  4 items       18,687.02  (0 flagged)
   31-60 days:  1 items         -740.00  (1 flagged)
   61-90 days:  4 items       -5,593.65  (4 flagged)
  ESCALATE  DEP_NOT_CREDITED      9,406.58   80 days  from 2026-06  Cogeco underground build — change order
  ESCALATE  UNID_DEBIT           -1,260.00   67 days  from 2026-06  PRE-AUTH DEBIT UNKNOWN ORIG 7719
  ESCALATE  UNID_CREDIT           2,150.00   66 days  from 2026-06  E-TRANSFER RECEIVED T4X99A
  FOLLOW_UP OS_CHECK            -15,890.23   63 days  from 2026-06  Vermeer Canada — drill head rebuild
  ESCALATE  UNID_DEBIT             -740.00   38 days  from 2026-07  PRE-AUTH DEBIT UNKNOWN ORIG 8841
```

**Point at the difference between the last two categories.** A check outstanding
for 63 days is a phone call to the payee. A deposit the books say exists that the
bank has never seen, 80 days on, is a missing $9,406 — possibly an error,
possibly a fraud. Age alone is just a number; the thresholds are what make it
actionable, and unexplained money escalates at 30 days while a slow check gets
60.

```bash
python src/controls.py
```

**The rollforward:** open at start + raised − cleared − written off = open at
end, for every period, with each period's closing figure equal to the next one's
opening figure. This is the control a reviewer asks for, and it's one SQL query.

**The subtle part worth saying out loud:** it's checked a third way too, against
what each run recorded at the time — because the first two checks are computed
from the same table, so an item *deleted* from it disappears from both sides of
the equation and still balances. The run's own count is the outside witness.

Open `output/reconciliation_report_aug2026.xlsx` → **Open Items (aged)** and
**Rollforward** tabs. *"Everything I've just shown at the command line is also in
the workbook, because the person doing the close doesn't run Python."*

---

## Act 7 — Try to cheat

> "The accounting rules aren't in my code. They're in the database."

```bash
python demo/try_to_cheat.py
```

This bypasses the engine and attacks the ledger with raw SQL. Expect every
attempt refused:

```
One-to-one matching
  blocked      match a bank row that is already matched
               -> UNIQUE constraint failed: match.run_id, match.gl_entry_id

Items must be real and honest
  blocked      raise an item against a transaction that does not exist
  blocked      raise an item that sits on both sides at once
  blocked      clear an item without recording where it cleared

History is append-only
  blocked      rewrite an item's history

Closed books stay closed
  blocked      slip an extra transaction into a closed period
  blocked      restate a closed period's item
  blocked      delete a closed period's run
```

**Take each one briefly:**

- *One-to-one matching* is a `UNIQUE` constraint. It's what forces a duplicate GL
  posting into the exception report — the bank has one payment, the GL has two
  identical entries, so one is left standing.
- *Clear without recording where* is the constraint that keeps the rollforward
  honest. An item can't be marked resolved without saying which period resolved
  it, so the arithmetic can't drift.
- *Closed books* are triggers, which is the point: they hold against a future
  version of my own code, a stray script, or someone at a SQL prompt.

**Then the nuance at the end of the output:** a closed period's items can still be
*resolved*. June closes at the start of July, but June's outstanding check clears
on July 20 — locking the period must not freeze the items it raised. That
distinction is a test in the repo.

---

## Act 8 — How I know it works

```bash
python -m pytest -q
```

Expect `104 passed`.

**Three layers, so a failure says what broke:** golden numbers (every exception
with its category), one named test per planted edge case
(`test_late_check_matched_by_number`, `test_recurring_payment_not_flagged_duplicate`),
and controls that prove the controls themselves can fail.

**Then the part worth volunteering:** a passing test proves nothing until you've
seen it fail, so I mutation-tested it — reintroduce a bug, check the named test
catches it. That's how I found two real problems. Breaking the rollforward's
write-off term changed nothing, because the status column recomputed the same
arithmetic separately, so a bug could pass a check that quietly calculated it a
second way. And deleting an item from the ledger balanced perfectly, which is
what led to the third rollforward check.

---

## Closing line

> "The first version reconciled a month. This one runs the close: items persist
> across periods, clear when they show up on a later statement, age into
> escalation when they don't, and a rollforward proves nothing was lost between
> months. The proof starts from the bank's and the ledger's stated balances, so
> it can actually fail — and the accounting rules live in database constraints,
> so they hold even if my Python is wrong."

---

## Questions you'll get, and the short answers

**"Why not just use Excel / a pandas merge?"**
A merge finds matches. It doesn't diagnose breaks, doesn't remember them, can't
age them, and has no control proving nothing was lost between months. The
matching is the easy 10%.

**"Why SQLite and not Postgres?"**
Distribution, not capability. A server and credentials mean a reviewer can't
clone and run. The schema ports with small changes, and Postgres would be right
for a real deployment — along with row-level permissions, since closing the
books is a segregation-of-duties question.

**"Is this machine learning?"**
No, and deliberately. Every classification is a rule that can be defended line
by line in an audit: keyword patterns plus the sign of the amount, check
numbers, distance from period end, document-number identity. "The model thought
so" is not an answer a reviewer can accept.

**"How would this handle real bank data?"**
The column mapping is the adapter; a QuickBooks *Transaction Detail by Account*
export maps directly. What would need real work: many-to-one matching for
batched deposits, and a config file per bank and ERP export format.

**"What was the hardest part?"**
Deciding what "open" means. Re-running July lost every clearing, because Pass 0
looked for items open *now* and its own previous run had already cleared them.
"Open as of the start of the period" is the correct definition, and a test found
it, not me.

---

## Reset between demos

```bash
del ledger.db                          # rm -f ledger.db
python src/reconcile.py --period 2026-06
```

Or run all three periods to get back to a finished close. `demo/try_to_cheat.py`
leaves the ledger as it found it, and `demo/break_proof.py` never touches it —
it works on a temporary copy.