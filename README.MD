### Domain specific terms : Banking 
General ledger :  central master book or digital record where a business writes down every single financial transaction

### Engine logic 
A three-pass matching engine that reconciles a bank statement against a general-ledger
cash extract and isolates every break into a multi-tab, close-ready Excel report —
each exception ranked by dollar exposure and tagged with a probable cause and the
action that clears it.

On the bundled June 2026 dataset it **reconciles 115 transactions and isolates
14 exceptions**, and the reconciliation proof on the Summary tab ties to **$0.00**.

```
Pass 1 — EXACT       same signed amount, same date                          → 95 matches
Pass 2 — TIMING      same amount, cleared within ±5 days                    → 12 matches
Pass 3 — TOLERANCE   amount within $0.99, ±7 days, fuzzy description match  →  8 matches
                                                            Total reconciled: 115
Everything left over → classified, ranked exception report                  →  14 breaks
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
transit*, *bank fee never booked*, *possible duplicate posting*, *unidentified
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
│   ├── bank_statement_jun2026.csv    # 122 rows — what the bank says
│   └── gl_cash_extract_jun2026.csv   # 122 rows — what QuickBooks says
├── output/
│   └── reconciliation_report_jun2026.xlsx   # 7-tab close-ready report
├── docs/
│   └── WALKTHROUGH.md       # accounting logic, design decisions, interview Q&A
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
Bank rows: 122   GL rows: 122
Pass 1 exact:      95
Pass 2 timing:     12
Pass 3 tolerance:   8
Total reconciled: 115
Exceptions:        14  (bank-only 7, GL-only 7)
```

Open `output/reconciliation_report_jun2026.xlsx` and start on the **Summary** tab.

## How the engine works

**Pass 1 — Exact.** Transactions are keyed on signed amount in integer cents (no
floating-point fuzz) and matched one-to-one when amount and date are identical.
This clears the bulk of the file instantly.

**Pass 2 — Timing.** A check written June 10 might clear June 13; an EFT settles a
day or two after it's booked. Pass 2 matches identical amounts whose dates fall
within ±5 days, taking the *nearest* date when several qualify. These aren't errors —
they're the normal life of a transaction — so they're matched, not flagged.

**Pass 3 — Tolerance + fuzzy description.** Rounding differences, FX cents,
keyed-in-cents errors. Pass 3 pairs transactions whose amounts differ by ≤ $0.99 and
whose dates are within ±7 days, but only when the descriptions actually resemble each
other (normalized token-overlap / sequence similarity ≥ 0.35 — so "EFT WESCO DIST
CANADA" pairs with "WESCO Distribution — invoice payment", but not with an unrelated
vendor that happens to be 40¢ away). The residual cents are carried to the Summary
proof as a pending write-off JE, so nothing silently disappears.

**Exception classification.** Whatever survives all three passes is a real break.
Instead of dumping them into one tab, the engine diagnoses each:

| Side | Pattern | Probable cause | Suggested action |
|---|---|---|---|
| Bank only | fee / service charge / NSF keywords | Bank charge not booked | Book JE: DR Bank Charges / CR Cash |
| Bank only | interest keywords | Interest not booked | Book JE: DR Cash / CR Interest Income |
| Bank only | unknown credit | Unidentified deposit | Trace with bank / AR before booking |
| Bank only | unknown debit | Possible unauthorized PAD | Investigate with bank |
| GL only | check no., near month end | Outstanding check | Carry as reconciling item |
| GL only | deposit on last GL day | Deposit in transit | Verify on July statement |
| GL only | identical to an already-matched entry | **Possible duplicate posting** | Reverse the duplicate JE |

Exceptions are ranked by absolute dollar amount — the $48K deposit in transit is
row 1, the $45 wire fee is row 14 — because that's the order a reviewer works in.

## Reading the report (7 tabs)

1. **Summary** — match counts per pass, match rate, exception exposure, and a formal
   reconciliation proof: bank balance → adjusted bank balance vs. GL balance →
   adjusted GL balance, tying to an unreconciled difference of **0.00**.
2. **Exceptions** — the working tab. Ranked, color-coded by cause category, with a
   suggested action per row.
3. **Matched — Exact / Timing / Tolerance** — full audit trail of every matched pair,
   including days-delta, amount-delta, and (for pass 3) the fuzzy match score.
4. **Bank Statement (source)** and **GL Extract (source)** — untouched inputs, so the
   workbook is self-contained for review or audit.

## Using it with real data

Point the CLI flags at your own files:

```bash
.venv/bin/python src/reconcile.py --bank my_bank.csv --gl my_gl.csv --out my_rec.xlsx
```

Expected columns — `Date, Description, Reference, Amount` (bank) and
`Date, Account, Memo, DocNo, Amount` (GL), with amounts signed from the cash
account's perspective (deposits +, disbursements −). A QuickBooks
*Transaction Detail by Account* export for the cash account maps directly.

Tune the knobs at the top of `src/reconcile.py`:

| Parameter | Default | Meaning |
|---|---|---|
| `TIMING_WINDOW_DAYS` | 5 | max clearing lag for pass 2 |
| `TOLERANCE_DOLLARS` | 0.99 | max amount difference for pass 3 |
| `TOLERANCE_WINDOW_DAYS` | 7 | max date spread for pass 3 |
| `FUZZY_THRESHOLD` | 0.35 | min description similarity for pass 3 |
| `OPENING_BALANCE` | 184,352.19 | opening cash balance for the proof |

## Design decisions worth knowing

- **Integer-cents matching.** Amounts are compared as `round(amount × 100)` integers,
  never floats — a reconciliation engine that trusts `0.1 + 0.2 == 0.3` will
  eventually lie to you.
- **Greedy one-to-one matching, strictest pass first.** Every bank row can consume at
  most one GL row, so a duplicate GL posting *cannot* hide behind the original — the
  second copy is forced out as an exception, which is exactly how the duplicate-payment
  test on this dataset gets caught.
- **Pass 3 requires description similarity, not just amount proximity.** Amount-only
  tolerance matching is how reconciliation tools create false matches; the fuzzy gate
  keeps "close in dollars" from being mistaken for "the same transaction."
- **Nothing disappears.** Pass-3 cent residuals flow to the proof as a pending
  write-off; unidentified items are shown as *pending identification*, not absorbed.
  The proof must tie to 0.00 or the report tells you it doesn't.

See [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) for the full accounting logic and a
set of interview-ready talking points, and
[docs/THE_NSF_BUG.md](docs/THE_NSF_BUG.md) for the story of the one bug this
project's own test data caught before it could ever misclassify a real transaction.