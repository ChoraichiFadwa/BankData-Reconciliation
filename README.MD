### Domain specific terms : Banking
General ledger :  central master book or digital record where a business writes down every single financial transaction

### Engine logic
A three-pass matching engine that reconciles a bank statement against a general-ledger
cash extract and isolates every break into a multi-tab, close-ready Excel report —
each exception ranked by dollar exposure and tagged with a probable cause and the
action that clears it.

On the bundled June 2026 dataset it **reconciles 118 transactions and isolates
18 exceptions**, and the reconciliation proof on the Summary tab, built from the
bank's and the GL's *stated* closing balances, ties to **$0.00**.

```
Pass 1 — EXACT       same signed amount, same date                          → 95 matches
Pass 2 — TIMING      same amount, cleared later:                            → 15 matches
                       checks by check number (any lag)          13
                       others within ±5 days, optimal assignment  2
Pass 3 — TOLERANCE   amount within $0.99, ±7 days, fuzzy description match  →  8 matches
                                                            Total reconciled: 118
Everything left over → classified, ranked exception report                  →  18 breaks
```

---

## Use case

The bank reconciliation is the control at the heart of every month-end close: prove
that what the bank says happened and what the books say happened are the same thing,
and explain every dollar of difference. Done by hand it means two spreadsheets, a
highlighter, and hours of eyeballing and the output is usually a single
undifferentiated "unmatched" list that still needs triage.

This engine automates the part that doesn't need judgment (finding the matches) so a
person's time goes only where judgment is needed (the breaks). Crucially, it builds
**the exception report an accountant actually works from**: each break arrives
pre-ranked by dollar exposure and pre-diagnosed — *outstanding check*, *deposit in
transit*, *bank fee never booked*, *customer cheque returned NSF*, *possible
duplicate posting*, *deposit the bank never received — investigate*, *unidentified
debit — investigate* — with the journal entry or follow-up that clears it.

Real-world fit: exactly the reconciliation work performed at month-end in an AP /
accounting-assistant role, scaled from "an afternoon" to "under a second."

## Repo layout

```
bank-to-gl-reconciliation/
├── src/
│   ├── generate_data.py     # builds the synthetic June 2026 dataset (deterministic, seed=42)
│   └── reconcile.py         # the three-pass engine + Excel report writer
├── data/
│   ├── bank_statement_jun2026.csv    # 127 rows — what the bank says
│   ├── gl_cash_extract_jun2026.csv   # 127 rows — what QuickBooks says
│   └── balances_jun2026.csv          # stated opening/closing balances (statement + trial balance)
├── output/
│   └── reconciliation_report_jun2026.xlsx   # 7-tab close-ready report
├── docs/
│   ├── WALKTHROUGH.md       # accounting logic, design decisions, interview Q&A
│   └── THE_NSF_BUG.md       # the bug the test data caught
├── requirements.txt
└── README.md
```

## Quickstart

```bash
cd bank-to-gl-reconciliation
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/generate_data.py     # regenerate the synthetic dataset (optional — CSVs are committed)
.venv/bin/python src/reconcile.py         # run the reconciliation, write the Excel report
```

Expected console output:

```
Bank rows: 127   GL rows: 127
Pass 1 exact:      95
Pass 2 timing:     15  (check no. 13, date window 2)
Pass 3 tolerance:   8
Total reconciled: 118
Exceptions:        18  (bank-only 9, GL-only 9)
Completeness Bank         0.00  OK
Completeness GL           0.00  OK
Unreconciled difference: 0.00  TIES
```

Open `output/reconciliation_report_jun2026.xlsx` and start on the **Summary** tab.

## How the engine works

**Pass 1 — Exact.** Amounts are parsed from the CSV text with `Decimal` into
integer cents (never through a float) and matched one-to-one when amount and
date are identical. This clears the bulk of the file instantly.

**Pass 2 — Timing.** A check written June 5 might clear June 16; an EFT settles a
day or two after it's booked. Checks are matched on check number and amount,
whatever the lag, because the check number is the strongest identifier the bank
gives you. Everything else is matched on identical amounts within ±5 days. When
several same-amount rows compete (recurring lease payments, say), the pairs are
assigned optimally, maximising the number of matches and then minimising total
lag, rather than greedily, because greedy nearest-date strands rows that had a
valid partner. These aren't errors — they're the normal life of a transaction —
so they're matched, not flagged.

**Pass 3 — Tolerance + fuzzy description.** Rounding differences, FX cents,
keyed-in-cents errors. Pass 3 pairs transactions whose amounts differ by ≤ $0.99,
have the same direction (money in or money out), and whose dates are within ±7
days, but only when the descriptions actually resemble each other (normalized
token-overlap / sequence similarity ≥ 0.35 — so "EFT WESCO DIST CANADA" pairs
with "WESCO Distribution — invoice payment", but not with an unrelated vendor
that happens to be 40¢ away). The best-scoring candidates are paired first. The
residual cents are carried to the Summary proof as a pending write-off JE, so
nothing silently disappears.

**Exception classification.** Whatever survives all three passes is a real break.
Instead of dumping them into one tab, the engine diagnoses each:

| Side | Pattern | Category | Suggested action |
|---|---|---|---|
| Bank only | fee / service charge keywords | `BANK_CHARGE` | Book JE: DR Bank Charges / CR Cash |
| Bank only | interest keywords, credit | `INTEREST_INC` | Book JE: DR Cash / CR Interest Income |
| Bank only | interest keywords, debit | `INTEREST_EXP` | Book JE: DR Interest Expense / CR Cash |
| Bank only | NSF / returned item (not a fee) | `NSF_RETURN` | Book JE: DR Accounts Receivable / CR Cash, contact customer |
| Bank only | unknown credit | `UNID_CREDIT` | Trace with bank / AR before booking |
| Bank only | unknown debit | `UNID_DEBIT` | Investigate with bank |
| GL only | check number | `OS_CHECK` | Carry as reconciling item |
| GL only | payment, near month end | `PMT_IN_TRANSIT` | Confirm on next statement |
| GL only | payment, mid-period | `PMT_NOT_DEBITED` | Investigate — was it sent? |
| GL only | deposit, near month end | `DIT` | Verify on next statement |
| GL only | deposit, mid-period | `DEP_NOT_CREDITED` | Investigate — was it deposited? |
| GL only | same document and amount as another entry | `DUPLICATE` | Reverse the duplicate JE |

The category code, not the display text, drives the proof and the report, so
rewording a message can never move a dollar to the wrong side of the rec.

Exceptions are ranked by absolute dollar amount — the $48K deposit in transit is
row 1, the $45 wire fee is row 18 — because that's the order a reviewer works in.

## Reading the report (7 tabs)

1. **Summary** — match counts per pass, match rate, exception exposure, input
   completeness controls (each file's rows must roll its stated opening balance
   to its stated closing balance), and a formal reconciliation proof: stated bank
   balance → adjusted bank balance vs. stated GL balance → adjusted GL balance,
   tying to an unreconciled difference of **0.00**.
2. **Exceptions** — the working tab. Ranked, color-coded by category, with a
   probable cause, a suggested action and the source-file row for each break.
3. **Matched — Exact / Timing / Tolerance** — full audit trail of every matched
   pair, including the match method (amount + date, check no., date window,
   fuzzy), days-delta, amount-delta, and the fuzzy match score for pass 3.
4. **Bank Statement (source)** and **GL Extract (source)** — untouched inputs, so the
   workbook is self-contained for review or audit.

## Using it with real data

Point the CLI flags at your own files:

```bash
.venv/bin/python src/reconcile.py --bank my_bank.csv --gl my_gl.csv \
    --balances my_balances.csv --out my_rec.xlsx
```

Expected columns — `Date, Description, Reference, Amount` (bank),
`Date, Account, Memo, DocNo, Amount` (GL), and
`Source, PeriodStart, PeriodEnd, OpeningBalance, ClosingBalance` (balances, one
row each for `BANK` and `GL`, taken from the statement header and the trial
balance). Amounts are signed from the cash account's perspective (deposits +,
disbursements −). A QuickBooks *Transaction Detail by Account* export for the
cash account maps directly.

Tune the knobs at the top of `src/reconcile.py`:

| Parameter | Default | Meaning |
|---|---|---|
| `TIMING_WINDOW_DAYS` | 5 | max clearing lag for pass 2 (non-check rows) |
| `TOLERANCE_DOLLARS` | 0.99 | max amount difference for pass 3 |
| `TOLERANCE_WINDOW_DAYS` | 7 | max date spread for pass 3 |
| `FUZZY_THRESHOLD` | 0.35 | min description similarity for pass 3 |
| `MONTH_END_DAYS` | 5 | how close to period end an item must be to count as "in transit" |
| `CHECK_REF` | `CHQ#1234` pattern | regex that extracts check numbers from references |

## Design decisions worth knowing

- **Integer-cents matching.** Amounts are parsed from text with `Decimal` into
  integer cents; floats appear only when values are written to Excel. A
  reconciliation engine that trusts `0.1 + 0.2 == 0.3` will eventually lie to you.
- **One-to-one matching, strictest pass first, optimal where rows compete.**
  Every bank row consumes at most one GL row, so a duplicate GL posting *cannot*
  hide behind the original. When same-amount rows compete on dates, the
  assignment is solved optimally instead of greedily.
- **Duplicates are identified by document, not by look-alike.** Same amount and
  same document number means duplicate; same vendor and amount with a different
  document is a recurring payment. The dataset includes three identical lease
  payments specifically to test this.
- **Pass 3 requires description similarity, not just amount proximity.**
  Amount-only tolerance matching is how reconciliation tools create false
  matches; the fuzzy gate keeps "close in dollars" from being mistaken for "the
  same transaction."
- **The proof is built on stated balances, not on the rows.** A proof computed
  from the rows themselves ties by construction and can never fail. This one
  starts from the bank statement's closing balance and the GL trial balance, and
  completeness controls check that each file's rows account for its balance.
  Delete one row and the proof breaks by exactly that amount.

See [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) for the full accounting logic and a
set of interview-ready talking points, and
[docs/THE_NSF_BUG.md](docs/THE_NSF_BUG.md) for the story of the one bug this
project's own test data caught before it could ever misclassify a real transaction.



## Version 1 : Phase 1 
python src/db.py --init
python src/db.py --load 2026-06
python -m pytest -q