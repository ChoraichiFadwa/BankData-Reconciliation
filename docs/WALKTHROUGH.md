# Walkthrough — the accounting logic, the engineering, and how to talk about it

This document explains the project at the depth an interviewer (or a reviewer at a
co-op employer) would probe: why each piece exists, what the accounting says, and
what you'd say when asked about it.

---

## 1. The business problem

Every month, a company's cash account in the general ledger and the bank's statement
for the same account tell two versions of the same story. They *should* agree, but
they never quite do, for three legitimate reasons and a few bad ones:

**Legitimate differences (timing):**
- **Outstanding checks** — the company wrote and booked a check; the payee hasn't
  cashed it yet. In the GL, not at the bank.
- **Deposits in transit** — revenue booked on June 30 that the bank credits July 2.
  In the GL, not at the bank.
- **Clearing lag** — a check booked June 10 clears June 13. In both files, on
  different dates.

**Differences that need a journal entry:**
- **Bank charges and interest** — fees, and interest earned or charged, that the
  bank recorded and the bookkeeper hasn't yet.
- **NSF returned items** — a customer's cheque bounced; the bank reversed the
  deposit. This reverses a receivable, it is not a fee.

**Differences that need investigation:**
- **Unidentified debits/credits** — a pre-authorized debit nobody recognizes, an
  e-transfer with no obvious customer. Could be a mistake; could be fraud.
- **Duplicate postings** — the same invoice paid (or booked) twice.
- **Book entries the bank never saw** — a deposit booked mid-month that never
  reached the bank, or a payment never debited. Near month end that's normal
  timing; two weeks later it's a problem.

The reconciliation's job is to (a) pair up everything that's genuinely the same
transaction, and (b) explain every remaining dollar with one of the categories above.
When the "adjusted bank balance" equals the "adjusted GL balance," the rec *ties* and
the close can proceed.

## 2. Why three passes, in this order

The passes run strictest-first, and each pass only sees what previous passes left
unmatched. That ordering is a correctness feature, not a style choice:

1. **Exact (amount + date)** clears ~75% of the file with zero risk of a false
   match. Anything this pass takes is unarguable.
2. **Timing (same amount, cleared later)** models how transactions actually
   clear. Amount is still exact — the only freedom given is the calendar.
   Checks match on check number regardless of lag, since the bank echoes the
   number. Other rows match within ±5 days; when several same-amount rows
   compete, the engine solves the assignment optimally (most pairs, then least
   total lag). Greedy nearest-date fails here: in the dataset, a Jun 10 lease
   debit would grab the Jun 11 booking and strand the Jun 15 debit seven days
   from the Jun 8 booking — two false exceptions.
3. **Tolerance + fuzzy (≤ $0.99, ±7 days, same direction, description
   similarity ≥ 0.35)** is the only pass allowed to pair *different* amounts, so
   it carries the highest false-match risk — which is why it runs last (fewest
   candidates left) and why it demands the descriptions agree. The similarity
   score is the max of a character-sequence ratio and token-set overlap on
   normalized text (uppercased, reference numbers stripped, noise words like
   EFT/PAD/LTD removed), so `PAD SHELL FLEET CARD SVC REF40241` and
   `Shell Fleet Card — invoice payment` score high while two unrelated vendors
   40 cents apart score near zero.

If you ran the passes in the opposite order, a sloppy tolerance match could steal a
row that had an exact partner, and the exact partner would then surface as a fake
exception. Strictest-first makes the result stable and defensible.

**One-to-one matching** matters for the same reason: each bank row consumes at
most one GL row. That's what forces the duplicate GL posting in the dataset out into
the open — the bank has *one* payment of $10,230.18, the GL has *two* identical
entries, so exactly one is left standing, and the classifier recognizes it as a
duplicate because it carries the same document number (CHQ#1042) and amount as
a matched entry. Recurring payments with the same amount but different
documents are deliberately *not* flagged.

## 3. The exception classifier

The design goal on the resume — *"the exception report an accountant actually works
from"* — means the report answers the reviewer's first three questions before they
ask: **How big is it? What probably caused it? What do I do about it?**

- **Ranked by |amount|** because review time should follow dollar risk. Rank 1 is a
  $48,310.77 deposit in transit; rank 18 is a $45 wire fee.
- **Cause** comes from cheap, explainable heuristics — keyword patterns plus the
  sign of the amount for fees, interest and NSF returns; check number vs.
  distance from period end for the GL side; same document and amount for
  duplicates; and an honest "unidentified — investigate" when nothing fits.
  Each break carries a category code (`DIT`, `OS_CHECK`, `NSF_RETURN`…) that
  drives the proof, separate from the human-readable text. No black boxes:
  every classification can be defended line-by-line in review.
- **Suggested action** is stated as the actual clearing entry where one exists
  (e.g. `DR 6220 Bank Charges / CR 1010 Cash`), and as the correct *process step*
  where booking would be premature (never book an unidentified credit to revenue —
  trace it first).

## 4. The reconciliation proof (Summary tab)

The proof follows the standard two-column bank rec format:

```
Ending balance per bank statement                    394,586.77
  add: deposits in transit                         + 61,075.08
  add: deposits not credited by bank (investigate) +  9,406.58
  less: outstanding checks                         − 29,821.53
  less: payments in transit                        −  1,286.43
Adjusted bank balance                                433,960.47

Ending balance per general ledger                    427,720.69
  add: bank charges not booked                     −    607.44
  add: interest not booked (net)                   −    397.21
  add: NSF returned deposits not booked            −  3,875.40
  add: unidentified bank items (pending ID)        +    890.00
  add back: duplicate posting to reverse           + 10,230.18
  add: pass-3 residuals (pending write-off)        −      0.35
Adjusted GL balance                                  433,960.47

Unreconciled difference                                    0.00
```

Three details worth noticing:

- **The starting balances are stated, not computed.** They come from the bank
  statement header and the GL trial balance, not from summing the rows. A proof
  built from the rows would tie by construction — every unmatched row lands on
  one side or the other, so the difference is always zero, whatever the data.
  Built from stated balances, a missing or extra row breaks the proof. Separate
  completeness controls show which file is off: deleting one $5,170.46 bank row
  produces a $5,170.46 break on both the control and the proof.
- **The pass-3 residual line.** The eight tolerance matches differ from their GL
  entries by a net −$0.35. A lazy tool would let those cents vanish inside "matched."
  Here they're surfaced as a pending write-off JE, because the proof must account for
  *every* cent of difference between the two files — that's the whole point of a rec.
- **Unidentified items are shown as pending, not solved.** The proof ties
  arithmetically, but the report is explicit that $890 net of it is awaiting
  identification. Tying is not the same as done; the report doesn't pretend otherwise.

## 5. The synthetic dataset

`generate_data.py` builds the scenario rather than downloading one, for three reasons:
no real company's bank data can be public; the breaks need to be *known* so the
engine's output can be verified against ground truth; and the generator doubles as a
spec of every failure mode the engine claims to handle.

The scenario is a telecom-infrastructure contractor's operating account (vendors like
WESCO, United Rentals, Brandt Tractor; progress-billing deposits from Rogers/Telus/
Bell/Cogeco) — June 2026, 127 bank rows vs. 127 GL rows, plus a balances file.
It's seeded (`seed=42`), so every run reproduces exactly 118 reconciled / 18
exceptions. The base scenario uses unique cent values so amount-based matching is
unambiguous. Five edge cases are then added on purpose, each targeting one
engine rule: a check clearing 11 days late, three vehicle leases at the same
amount, interest charged on the line of credit, a customer cheque returned NSF,
and a mid-month deposit the bank never received. Run against the original
engine, these produced four false exceptions and seven wrong diagnoses; the
current engine gets all of them right.

## 6. Interview Q&A

**"Walk me through what happens when you run it."**
Load both CSVs and the stated balances → convert amounts to integer cents →
pass 1 pairs identical amount+date → pass 2 pairs checks by check number and
other identical amounts within a 5-day clearing window → pass 3 pairs
near-amounts with similar descriptions → whatever's left is classified, ranked,
and written to a 7-tab Excel workbook whose proof, starting from the stated
balances, ties to zero.

**"Why integer cents?"**
Binary floats can't represent most decimal fractions; `0.1 + 0.2 != 0.3` in float
math. In a matching engine that compares money for equality thousands of times,
float comparison eventually produces a wrong answer silently. Cents-as-integers makes
equality exact. Amounts are parsed from the CSV text with `Decimal`, so they
never pass through a float at all.

**"How do you avoid false matches in the fuzzy pass?"**
Four independent gates — amount within $0.99, dates within 7 days, same
direction (money in vs. out), and description similarity above threshold on
normalized text — plus the structural protections of running last and
best-score-wins. And the pass records its score in the report, so a reviewer
can audit every fuzzy pairing.

**"What would you change for production use?"**
Several items from the first version's list are now built: check-number
matching, optimal assignment for competing same-amount rows, and a proof on
stated balances. Still open: many-to-one matching for batched deposits (three
GL receipts settling as one bank credit); a config file per bank/ERP format;
and persistence of reconciling items month over month, so outstanding checks
clear automatically on the next statement and age when they don't — that's
the SQL ledger I'm building next.

**"How long did the manual version take, and what does this change?"**
A ~120-transaction manual rec is an afternoon of tick-and-tie plus triage. The engine
runs in under a second, and — more importantly — its output starts where the human's
judgment is actually needed: eighteen diagnosed breaks instead of two raw files.

**"What if two payments have the same amount?"**
That's the case greedy matchers get wrong, so the dataset includes it on
purpose: three identical vehicle leases. Same-amount rows are assigned
optimally by date, and a leftover one isn't called a duplicate unless it
repeats the same document number — a recurring payment is not an error.

**"How do you know your proof isn't circular?"**
The first version's proof was: it was built from the rows, so it tied to zero
by construction. The proof now starts from the statement's and the trial
balance's closing balances, and completeness controls check each file's rows
against them. I tested it by deleting a row — the proof breaks by exactly
that row's amount.

## 7. File-by-file

| File | What it does |
|---|---|
| `src/generate_data.py` | Deterministic scenario builder: 95 exact pairs, 15 timing pairs (13 checks, 2 same-amount leases), 8 fuzzy pairs, 9 bank-only and 9 GL-only breaks (incl. one planted duplicate), five targeted edge cases, and the stated balances. Writes three CSVs. |
| `src/reconcile.py` | Loads CSVs and balances, runs the three passes (`run_matching`), classifies leftovers into category codes (`classify_exceptions`), builds the proof and completeness controls (`build_proof`), writes the styled workbook (`build_report`). `reconcile()` runs everything except the report, for tests. All tunable thresholds are named constants at the top. |
| `data/*.csv` | The three source files, committed so the project runs (and the report regenerates) without regenerating data. |
| `output/reconciliation_report_jun2026.xlsx` | The deliverable: Summary, Exceptions, three Matched tabs, two source tabs. |