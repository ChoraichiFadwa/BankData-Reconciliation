# Walkthrough — the accounting logic, the engineering, and how to talk about it

This document explains the project at the depth an interviewer (or a reviewer at
a prospective employer) would probe: why each piece exists, what the accounting
says, and what you'd say when asked about it.

---

## 1. The business problem

Every month, a company's cash account in the general ledger and the bank's
statement for the same account tell two versions of the same story. They
*should* agree, but they never quite do:

**Legitimate differences (timing):**
- **Outstanding checks** — written and booked; the payee hasn't cashed them yet.
- **Deposits in transit** — revenue booked June 30 that the bank credits July 2.
- **Clearing lag** — a check booked June 10 clears June 13.

**Differences that need a journal entry:**
- **Bank charges and interest** — the bank recorded something the bookkeeper
  hasn't. Note that interest can be *earned* (a credit, income) or *charged* on
  a line of credit (a debit, expense): the sign decides the entry.
- **NSF returned items** — a customer's cheque bounced and the bank reversed the
  deposit. This reverses a receivable; only the separate NSF *fee* is a charge.

**Differences that need investigation:**
- **Unidentified debits and credits** — a pre-authorized debit nobody
  recognizes. Could be a mistake; could be fraud.
- **Duplicate postings** — the same invoice booked twice.
- **Book entries the bank never saw** — a deposit booked mid-month that never
  reached the bank. Near month end that is normal timing; two weeks later it is
  a problem.

The job is to pair everything that is genuinely the same transaction, and to
explain every remaining dollar with one of the categories above. When the
adjusted bank balance equals the adjusted GL balance, the rec *ties*.

**And then the month ends, and the work isn't over.** Those explanations are
open items with a future. That second half is what sections 5–8 are about.

## 2. Why the passes run in this order

Strictest first, each pass seeing only what earlier passes left. That ordering
is a correctness feature, not a style choice.

1. **Exact (amount + date)** clears about three-quarters of the file with zero
   risk of a false match.
2. **Timing** models how transactions actually clear. Checks match on check
   number regardless of lag — the bank echoes the number, which is stronger
   evidence than any date window, and it is the only thing that pairs a check
   written June 5 and cleared June 16. Other rows match on identical amounts
   within ±5 days; when several same-amount rows compete, the engine solves the
   assignment optimally (most pairs, then least total lag). Greedy
   nearest-date fails here: in the dataset, a Jun 10 lease debit grabs the Jun 11
   booking and strands the Jun 15 debit seven days from the Jun 8 booking — two
   false exceptions.
3. **Tolerance + fuzzy** is the only pass allowed to pair *different* amounts, so
   it carries the highest false-match risk: it runs last, when fewest candidates
   remain, and demands four gates (≤ $0.99, ±7 days, same direction, description
   similarity ≥ 0.35).

Run in the opposite order, a sloppy tolerance match could steal a row that had
an exact partner, and that partner would surface as a fake exception.

**One-to-one matching** matters for the same reason: each bank row consumes at
most one GL row. That is what forces the duplicate GL posting into the open —
the bank has one payment of $10,230.18, the GL has two identical entries, so
exactly one is left standing, and the classifier recognizes it as a duplicate
because it carries the same document number and amount as a matched entry.
Recurring payments with the same amount but different documents are deliberately
*not* flagged.

## 3. The exception classifier

The design goal — *the exception report an accountant actually works from* —
means answering the reviewer's first three questions before they ask: **How big
is it? What probably caused it? What do I do about it?**

- **Ranked by |amount|**, because review time should follow dollar risk.
- **Cause** comes from cheap, explainable heuristics: keyword patterns plus the
  sign of the amount for fees, interest and NSF returns; check number versus
  distance from period end on the GL side; same document and amount for
  duplicates; and an honest "unidentified — investigate" when nothing fits.
- **Category codes** (`DIT`, `OS_CHECK`, `NSF_RETURN`…) are stored separately
  from the human-readable text, and it is the codes that drive the proof, the
  aging thresholds and the carry-forward rules. Rewording a message cannot move
  a dollar.
- **Suggested action** is the actual clearing entry where one exists, and the
  correct *process step* where booking would be premature — never book an
  unidentified credit to revenue; trace it first.

## 4. The proof, and why it can fail

The proof follows the standard two-column bank rec: stated bank closing balance
adjusted for deposits in transit and outstanding checks, stated GL closing
balance adjusted for unbooked items, duplicates and residuals, and the two must
meet.

Three things worth noticing:

- **The starting balances are stated, not computed.** They come from the bank
  statement header and the GL trial balance. The first version of this project
  computed both sides as `opening + sum(rows)`, which meant every unmatched row
  landed on one side or the other and the difference cancelled to zero *for any
  input whatsoever*. It was circular — it could not fail. Built on stated
  balances, deleting one row breaks the proof by exactly that row's amount, and
  separate completeness controls say which file is short.
- **The pass-3 residual is a reconciling item, not a footnote.** Eight tolerance
  matches differ from their GL entries by a net −$0.35. A lazy tool lets those
  cents vanish inside "matched". Here the residual is an item in its own right
  that stays on the rec until the write-off JE is booked — which is also why the
  next period cannot silently inherit it.
- **Unidentified items are shown as pending, not solved.** The proof ties
  arithmetically while stating plainly that some of it is awaiting
  identification. Tying is not the same as done.

## 5. Why the tool needed a memory

The engine was a function: two files in, one report out. Everything it learned
died when the process exited. Questions it could not answer:

- What was still open at the end of June?
- Did this July bank row clear a June item, or is it new?
- How long has this check been outstanding?
- Did anything get lost between June's closing items and July's opening items?
- What was run for June, with which thresholds, and has anyone changed it since?

Each is a question about state that survives between runs.

**The concrete failure:** reconcile July with no memory of June and you get 19
exceptions instead of 10. Fourteen are false — the bank rows that cleared June's
checks have no partner in the July GL, so they look like unidentified debits,
and the JEs the bookkeeper posted for June's charges look like payments the bank
never debited. The proof misses by $33,133.92, which is exactly the gap between
the bank's and the GL's opening balances. That failure is a permanent test:
`test_july_without_pass_0_is_wrong`.

## 6. Why SQL, specifically

What is needed is persistent, queryable, *constrained* state.

- **CSVs or a JSON file of open items** would store it, but nothing stops an item
  being written twice or a "cleared" item pointing nowhere, and every question
  ("open items over 60 days by cause") becomes a Python loop you write and test
  yourself. That is a weak database, reimplemented.
- **Excel** adds hand-editability, which is fatal for an audit trail.
- **Parquet or a dataframe store** is built for analytics: no uniqueness, no
  foreign keys, no in-place status update, no transactions.
- **A document store** fits badly, because the data is relational — matches
  reference two transactions, items reference a period and a run, and joins are
  the main operation.
- **Postgres** would be right for a real deployment and wrong for a portfolio
  repo: a server and credentials mean a reviewer can no longer clone and run.
  SQLite is one file, in the standard library, and the schema ports with small
  changes.

What SQL gives concretely: constraints as guarantees (one-to-one matching is a
`UNIQUE`, not a convention); transactions, so a run lands whole or not at all;
declarative queries for pass 0, aging and the rollforward; and triggers, so a
closed period is locked against a future version of this code, not just against
the engine.

## 7. Pass 0 — the carry-forward

The only pass whose input is the ledger rather than a file. Four ways an item
resolves:

1. a GL-side item clears when the **bank** finally shows it (same amount, and the
   same check number when there is one);
2. a bank-side item clears when the bookkeeper **books** it;
3. a duplicate clears when it is **reversed** — same amount, opposite sign, same
   side;
4. a residual clears when its **write-off JE** is booked.

**The SQL returns candidates; Python decides.** One item can have several
candidates and one transaction can suit several items, so the join is
declarative and the one-to-one assignment — strictest rule first, a check number
beating a bare amount — is Python. Neither tool does the other's job.

**Consumed rows are withheld from passes 1–3 but still counted in the
completeness controls**, because they belong to an earlier period's item while
remaining genuinely rows of this month's file.

**Resolution is recorded on the item, not as a match row.** A duplicate reversal
pairs two GL rows and a residual has no source row at all, so a `match` table
requiring one row from each side is the wrong home. Each item records the
period, the run and the exact transaction that resolved it, plus an event.

**"Open" means open as of the start of the period.** This was found by a test:
rerunning July lost all 15 of its clearings, because pass 0 looked for items open
*now* and its own previous run had already cleared them. It now also considers
items resolved by this same period, which a rerun takes back.

## 8. The controls

- **Rollforward.** Open at start + raised − cleared − written off = open at end.
  It is checked three ways: against the items actually open, against the next
  period's opening figure, and against what the run recorded at the time. The
  third matters because the first two are computed from the same table — an item
  *deleted* from it vanishes from both sides of the equation and still balances.
  The run's own count is the outside witness.
- **Aging.** Buckets as of period end, with thresholds attached, because age
  alone is just a number: an outstanding check past 60 days needs chasing and
  past 180 is stale-dated, while unexplained money or money the bank never saw
  escalates at 30. Escalations are written to the item once, with an event, so
  "escalated in July" survives a later change of thresholds.
- **Period locking.** Closing sets triggers rejecting new transactions,
  deletions, item restatements and run deletions. Crucially it does *not* freeze
  the items the period raised: June's check still clears in July with June
  closed. Closing also refuses if the proof doesn't tie or a file is incomplete,
  with a `force` escape that records the override; reopening demands a reason
  and leaves a `period_event` trail.

## 9. The synthetic dataset

`generate_data.py` builds the scenario rather than downloading one: no real
company's bank data can be public; the breaks need to be *known* so output can be
verified against ground truth; and the generator doubles as a spec of every
failure mode the engine claims to handle.

Three chained months for a telecom-infrastructure contractor, each opening where
the last closed *per side* — so from July on the bank and the GL open at
different balances, which is the gap the carry-forward has to explain.

The base scenario uses unique cent values so amount matching is unambiguous.
Ambiguity and edge cases are then added deliberately: a check clearing 11 days
late, three vehicle leases at the same amount, interest charged on the line of
credit, a customer cheque returned NSF, a mid-month deposit the bank never
received. Run against the *original* engine these produced four false exceptions
and seven wrong diagnoses — which is how we know the bugs they test for were
real.

Across periods: most June items clear in July, but one check is still
outstanding at the end of August (63 days), the deposit never received is 80 days
old, and two unidentified items were never traced.

## 10. Interview Q&A

**"Walk me through what happens when you run it."**
Load the period into the ledger → pass 0 clears whatever earlier periods left
open that this month's transactions resolve → passes 1–3 match the remaining
rows, strictest first → whatever is left is classified, ranked and raised as
reconciling items → the aging thresholds are applied → a nine-tab workbook is
written whose proof, built from the stated balances, ties to zero → the run,
its matches and its items are saved in one transaction.

**"Why integer cents?"**
Binary floats can't represent most decimal fractions; `0.1 + 0.2 != 0.3`. In an
engine that compares money for equality thousands of times, float comparison
eventually produces a wrong answer silently. Amounts are parsed from the CSV
text with `Decimal`, so they never pass through a float at all.

**"How do you avoid false matches in the fuzzy pass?"**
Four independent gates — amount within $0.99, dates within 7 days, same
direction, and description similarity on normalized text — plus the structural
protection of running last, with best-score-first assignment. The score is
recorded in the report, so a reviewer can audit every fuzzy pairing.

**"What if two payments have the same amount?"**
That's the case greedy matchers get wrong, so the dataset includes it on
purpose: three identical vehicle leases. Same-amount rows are assigned optimally
by date, and a leftover one isn't called a duplicate unless it repeats the same
document number — a recurring payment is not an error.

**"How do you know your proof isn't circular?"**
The first version's was: it was built from the rows, so it tied by construction.
It now starts from the statement's and the trial balance's closing balances,
with completeness controls checking each file's rows against them. I tested it
by deleting a row — the proof breaks by exactly that row's amount.

**"Why add a database to a reconciliation tool?"**
Because a reconciliation is a continuous control, not a monthly file comparison.
A check written June 28 and cleared July 20 is invisible to any date window and
absent from the July GL entirely — the only thing that knows it's still open is
the ledger. Reconciling July without it produces fourteen false exceptions and a
proof that misses by the opening gap; that comparison is a test in the repo.

**"Why SQLite rather than Postgres?"**
For a repo a reviewer should be able to clone and run, a server and credentials
are a barrier, and SQLite speaks the same SQL. The schema ports to Postgres with
small changes. The choice is about distribution, not about capability.

**"What stops an item being lost between months?"**
The rollforward, checked three ways: its own arithmetic, continuity with the
next period's opening figure, and agreement with what the run recorded at the
time. The third is the one that catches a deletion, because the first two are
computed from the same table and a deleted item disappears consistently from
both sides.

**"What would you change for production use?"**
Many-to-one matching for batched deposits (three GL receipts settling as one
bank credit); a configuration file per bank and ERP export format; Postgres with
row-level permissions, since closing the books is a segregation-of-duties
question as much as a technical one; and a review queue, so escalated items are
assigned to a person rather than just flagged.

**"How long did the manual version take, and what does this change?"**
A ~120-transaction manual rec is an afternoon of tick-and-tie plus triage. The
engine runs in under a second, and — more importantly — its output starts where
judgment is actually needed: diagnosed breaks instead of two raw files, and a
list of what is still open from previous months, already aged.

## 11. File by file

| File | What it does |
|---|---|
| `src/generate_data.py` | Deterministic three-month scenario builder, including every planted edge case and the stated balances. Writes nine CSVs. |
| `src/reconcile.py` | Matching passes, classification into category codes, the proof and completeness controls, and the Excel report. `reconcile()` runs everything except the report, for tests. |
| `src/db.py` | Creates the ledger from `sql/`, opens connections with foreign keys on, loads a period idempotently. |
| `src/carryforward.py` | Pass 0: runs the candidate query and assigns resolutions one to one. Decides; writes nothing. |
| `src/store.py` | Persists a run, its matches, the items it raised and the resolutions pass 0 found — in one transaction. Also `write_off()`. |
| `src/controls.py` | Rollforward, aging with escalation, closing and reopening periods, plus a CLI. |
| `sql/schema.sql` | Eight tables, the constraints that carry the accounting rules, and the period-locking triggers. |
| `sql/pass0_carryforward.sql` | The carry-forward candidate query. |
| `sql/rollforward.sql` | The control views. |
| `sql/aging.sql` | Ages and flags open items as of a period end. |
| `tests/` | 104 tests across the engine, the ledger, persistence, carry-forward and the controls. |