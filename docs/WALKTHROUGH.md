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
- **Bank charges, interest, NSF fees** — the bank did something the bookkeeper
  hasn't recorded yet. At the bank, not in the GL.

**Differences that need investigation:**
- **Unidentified debits/credits** — a pre-authorized debit nobody recognizes, an
  e-transfer with no obvious customer. Could be a mistake; could be fraud.
- **Duplicate postings** — the same invoice paid (or booked) twice.

The reconciliation's job is to (a) pair up everything that's genuinely the same
transaction, and (b) explain every remaining dollar with one of the categories above.
When the "adjusted bank balance" equals the "adjusted GL balance," the rec *ties* and
the close can proceed.

## 2. Why three passes, in this order

The passes run strictest-first, and each pass only sees what previous passes left
unmatched. That ordering is a correctness feature, not a style choice:

1. **Exact (amount + date)** clears ~80% of the file with zero risk of a false
   match. Anything this pass takes is unarguable.
2. **Timing (amount, ±5 days)** models how transactions actually clear. Amount is
   still exact — the only freedom given is the calendar. Nearest-date-wins prevents
   a check from matching a coincidentally equal amount two weeks away when a
   same-week candidate exists.
3. **Tolerance + fuzzy (≤ $0.99, ±7 days, description similarity ≥ 0.35)** is the
   only pass allowed to pair *different* amounts, so it carries the highest
   false-match risk — which is why it runs last (fewest candidates left) and why it
   demands the descriptions agree. The similarity score is the max of a
   character-sequence ratio and token-set overlap on normalized text (uppercased,
   reference numbers stripped, noise words like EFT/PAD/LTD removed), so
   `PAD SHELL FLEET CARD SVC REF40241` and `Shell Fleet Card — invoice payment`
   score high while two unrelated vendors 40 cents apart score near zero.

If you ran the passes in the opposite order, a sloppy tolerance match could steal a
row that had an exact partner, and the exact partner would then surface as a fake
exception. Strictest-first makes the result stable and defensible.

**One-to-one greedy matching** matters for the same reason: each bank row consumes at
most one GL row. That's what forces the duplicate GL posting in the dataset out into
the open — the bank has *one* payment of $10,230.18, the GL has *two* identical
entries, so exactly one is left standing, and the classifier recognizes it as
identical to an already-matched entry → "possible duplicate posting."

## 3. The exception classifier

The design goal on the resume — *"the exception report an accountant actually works
from"* — means the report answers the reviewer's first three questions before they
ask: **How big is it? What probably caused it? What do I do about it?**

- **Ranked by |amount|** because review time should follow dollar risk. Rank 1 is a
  $48,310.77 deposit in transit; rank 14 is a $45 wire fee.
- **Cause** comes from cheap, explainable heuristics — keyword patterns for
  fees/interest, document type + month-end proximity for outstanding checks vs.
  deposits in transit, identity-with-a-matched-row for duplicates, and an honest
  "unidentified — investigate" when nothing fits. No black boxes: every
  classification can be defended line-by-line in review.
- **Suggested action** is stated as the actual clearing entry where one exists
  (e.g. `DR 6220 Bank Charges / CR 1010 Cash`), and as the correct *process step*
  where booking would be premature (never book an unidentified credit to revenue —
  trace it first).

## 4. The reconciliation proof (Summary tab)

The proof follows the standard two-column bank rec format:

```
Ending balance per bank statement            407,259.49
  add: deposits in transit                  + 61,075.08
  less: outstanding checks                  − 29,821.53
Adjusted bank balance                        438,513.04

Ending balance per general ledger            427,785.78
  add: bank charges / interest (net)        −    392.57
  add back: duplicate posting to reverse    + 10,230.18
  add: unidentified items (pending ID)      +    890.00
  add: pass-3 residuals (pending write-off) −      0.35
Adjusted GL balance                          438,513.04

Unreconciled difference                            0.00
```

Two details worth noticing:

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
Bell/Cogeco) — June 2026, 122 bank rows vs. 122 GL rows. It's seeded (`seed=42`), so
every run reproduces exactly 115 reconciled / 14 exceptions. Amounts are generated
with unique cent values so amount-based matching is unambiguous by construction —
except where the dataset *deliberately* breaks that rule to plant the duplicate.

## 6. Interview Q&A

**"Walk me through what happens when you run it."**
Load both CSVs → convert amounts to integer cents → pass 1 pairs identical
amount+date → pass 2 pairs identical amounts within a 5-day clearing window →
pass 3 pairs near-amounts with similar descriptions → whatever's left is classified,
ranked, and written to a 7-tab Excel workbook whose Summary proof ties to zero.

**"Why integer cents?"**
Binary floats can't represent most decimal fractions; `0.1 + 0.2 != 0.3` in float
math. In a matching engine that compares money for equality thousands of times,
float comparison eventually produces a wrong answer silently. Cents-as-integers makes
equality exact.

**"How do you avoid false matches in the fuzzy pass?"**
Three independent gates — amount within $0.99, dates within 7 days, and description
similarity above threshold on normalized text — plus the structural protections of
running last and best-score-wins. And the pass records its score in the report, so a
reviewer can audit every fuzzy pairing.

**"What would you change for production use?"**
Reference/check-number matching as a pass 0 (many banks echo the check number —
that's even stronger than amount+date); many-to-one matching for batched deposits
(three GL receipts settling as one bank credit); a config file per bank/ERP format;
persistence of cleared-item state month over month so outstanding checks age
automatically; and a small test suite pinning the known dataset to its expected
output as a regression check.

**"How long did the manual version take, and what does this change?"**
A ~120-transaction manual rec is an afternoon of tick-and-tie plus triage. The engine
runs in under a second, and — more importantly — its output starts where the human's
judgment is actually needed: fourteen diagnosed breaks instead of two raw files.

## 7. File-by-file

| File | What it does |
|---|---|
| `src/generate_data.py` | Deterministic scenario builder: 95 exact pairs, 12 timing pairs, 8 fuzzy pairs, 7 bank-only breaks, 7 GL-only breaks (incl. one planted duplicate). Writes the two CSVs. |
| `src/reconcile.py` | Loads CSVs, runs the three passes (`run_matching`), classifies leftovers (`classify_exceptions`), writes the styled workbook (`build_report`). All tunable thresholds are named constants at the top. |
| `data/*.csv` | The two source files, committed so the project runs (and the report regenerates) without regenerating data. |
| `output/reconciliation_report_jun2026.xlsx` | The deliverable: Summary, Exceptions, three Matched tabs, two source tabs. |
