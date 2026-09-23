# Bank-to-GL Reconciliation

A month-end close, not a one-off match. The engine reconciles a bank statement
against a general-ledger cash extract, isolates every break into a close-ready
Excel report, and — because a reconciliation is a continuous control rather than
a monthly file comparison — **remembers**: every break becomes a reconciling
item in a SQL ledger that clears when a later period resolves it, ages when it
doesn't, and rolls forward under a control proving nothing was lost between
months.

On the bundled three-month dataset (June–August 2026) every period's proof ties
to **$0.00**, June's 19 items are cleared down to 4 over two months, and the
rollforward balances throughout.

```
Pass 0 — CARRY-FORWARD   prior periods' open items, cleared by this period   (SQL: the ledger)
Pass 1 — EXACT           same signed amount, same date
Pass 2 — TIMING          checks by check number; others within ±5 days, assigned optimally
Pass 3 — TOLERANCE       amount within $0.99, ±7 days, fuzzy description match
Everything left over  →  classified, ranked, and raised as a reconciling item
```

| Period | Bank / GL rows | Pass 0 | Passes 1–3 | Items raised | Open at close | Proof |
|---|---|---|---|---|---|---|
| 2026-06 | 127 / 127 | — | 95 / 15 / 8 | 19 | 19 | 0.00 |
| 2026-07 | 91 / 93 | 15 cleared | 70 / 8 / 4 | 6 | 10 | 0.00 |
| 2026-08 | 60 / 62 | 5 cleared | 50 / 5 / 2 | 4 | 9 | 0.00 |

---

## Use case

The bank reconciliation is the control at the heart of every month-end close:
prove that what the bank says happened and what the books say happened are the
same thing, and explain every dollar of difference. Done by hand it means two
spreadsheets, a highlighter, and hours of eyeballing — and the output is usually
a single undifferentiated "unmatched" list that still needs triage.

This project automates the part that needs no judgment (finding the matches) so
a person's time goes only where judgment is needed (the breaks), and it builds
**the exception report an accountant actually works from**: each break ranked by
dollar exposure, diagnosed with a probable cause, and paired with the journal
entry or follow-up that clears it.

The second half of the job is the part most tools skip. June's breaks are not
results, they are open items with a future: most clear in July, one check won't
clear for months and has to age, one deposit may never clear and has to be
written off. That is what the ledger is for.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python src/generate_data.py            # rebuild the synthetic dataset (optional)
.venv/bin/python src/reconcile.py --period 2026-06
.venv/bin/python src/reconcile.py --period 2026-07
.venv/bin/python src/reconcile.py --period 2026-08
.venv/bin/python -m pytest -q                    # 104 tests
```

Each command loads the period into the ledger, carries forward what earlier
periods left open, reconciles the rest, applies the aging thresholds, writes
`output/reconciliation_report_<period>.xlsx`, and saves the run.

July's output, abbreviated:

```
Pass 0 carry-forward: cleared 15 prior items, 4 still open
Pass 0 carryfwd:   15
Pass 1 exact:      70
Pass 2 timing:      8  (check no. 8, date window 0)
Pass 3 tolerance:   4
Open items:        10  (bank-only 5, GL-only 5)
Completeness Bank          0.00  OK
Completeness GL            0.00  OK
Unreconciled difference: 0.00  TIES

  period   opening  raised  cleared  w/off  closing        exposure  status
 2026-06         0      19        0      0       19       25,153.12  BALANCED
 2026-07        19       6       15      0       10       21,019.94  BALANCED
```

### The controls

```bash
.venv/bin/python src/controls.py                          # rollforward, every period
.venv/bin/python src/controls.py --aging 2026-08          # open items, aged and flagged
.venv/bin/python src/controls.py --close 2026-06 --note "signed off by JT"
.venv/bin/python src/controls.py --reopen 2026-06 --reason "restating a misposted invoice"
.venv/bin/python src/controls.py --history                # every close and reopen
```

### Without a database

The engine still runs on three files, with no ledger and no carry-forward. This
is also the control that shows what the memory buys:

```bash
.venv/bin/python src/reconcile.py --bank data/bank_statement_jul2026.csv \
    --gl data/gl_cash_extract_jul2026.csv --balances data/balances_jul2026.csv
```

July reconciled this way reports 19 exceptions instead of 10 — fourteen of them
false, June's resolved items reappearing as "unidentified" debits and credits —
and the proof misses by $33,133.92, exactly the gap between the bank's and the
GL's opening balances.

## Repo layout

```
bank-to-gl-reconciliation/
├── src/
│   ├── generate_data.py     # deterministic three-month dataset (seed=42)
│   ├── reconcile.py         # matching passes, classification, proof, Excel report
│   ├── db.py                # the ledger: create, connect, load a period
│   ├── carryforward.py      # pass 0: resolve prior periods' open items
│   ├── store.py             # persist a run, its matches and its items
│   └── controls.py          # rollforward, aging, closing the books (+ CLI)
├── sql/
│   ├── schema.sql           # 8 tables, the constraints, the locking triggers
│   ├── pass0_carryforward.sql
│   ├── rollforward.sql      # the control views
│   └── aging.sql
├── data/                    # three periods × (bank, GL, stated balances)
├── output/                  # one report per period
├── tests/                   # 104 tests
└── docs/
    ├── WALKTHROUGH.md       # accounting logic, design decisions, interview Q&A
    ├── SCHEMA.md            # the ledger, table by table
    └── THE_NSF_BUG.md
```

## How the passes work

**Pass 0 — Carry-forward.** Runs before everything else, and is the only pass
whose input is not a file. It asks the ledger what is still open from earlier
periods and finds this period's transactions that resolve those items: a GL-side
item (outstanding check, payment or deposit in transit) clears when the bank
finally shows it; a bank-side item (charge, interest, NSF return, unidentified)
clears when the bookkeeper books it; a duplicate clears when it is reversed; a
pass-3 residual clears when its write-off JE is booked. Rows consumed this way
are withheld from the passes below — they belong to an earlier period's item —
but still count in the completeness controls, because they are genuinely rows of
this month's file.

**Pass 1 — Exact.** Amounts are parsed from the CSV text with `Decimal` into
integer cents (never through a float) and matched one-to-one when amount and
date are identical.

**Pass 2 — Timing.** Checks are matched on check number whatever the clearing
lag, because the number is stronger evidence than a date window. Everything else
is matched on identical amounts within ±5 days; when several same-amount rows
compete, the pairs are assigned optimally (most pairs, then least total lag)
rather than greedily, because greedy nearest-date strands rows that had a valid
partner.

**Pass 3 — Tolerance + fuzzy.** Amounts within $0.99, same direction, dates
within ±7 days, and only where the descriptions actually resemble each other
(normalized similarity ≥ 0.35). Residual cents become a reconciling item in
their own right, so nothing disappears silently.

## Exception classification

| Side | Pattern | Category | Suggested action |
|---|---|---|---|
| Bank only | fee / service charge keywords | `BANK_CHARGE` | Book JE: DR Bank Charges / CR Cash |
| Bank only | interest keywords, credit | `INTEREST_INC` | Book JE: DR Cash / CR Interest Income |
| Bank only | interest keywords, debit | `INTEREST_EXP` | Book JE: DR Interest Expense / CR Cash |
| Bank only | NSF / returned item (not a fee) | `NSF_RETURN` | Book JE: DR A/R / CR Cash, contact customer |
| Bank only | unknown credit / debit | `UNID_CREDIT` / `UNID_DEBIT` | Trace before booking / investigate |
| GL only | check number | `OS_CHECK` | Carry as reconciling item |
| GL only | payment, near month end / mid-period | `PMT_IN_TRANSIT` / `PMT_NOT_DEBITED` | Confirm next statement / investigate |
| GL only | deposit, near month end / mid-period | `DIT` / `DEP_NOT_CREDITED` | Verify next statement / investigate |
| GL only | same document and amount as another entry | `DUPLICATE` | Reverse the duplicate JE |
| — | cents left over by pass 3 | `RESIDUAL` | Book the write-off JE |

The category code, not the display text, drives the proof, the aging thresholds
and the carry-forward rules, so rewording a message can never move a dollar to
the wrong side of the rec.

## The ledger

Eight tables (see [docs/SCHEMA.md](docs/SCHEMA.md)). Each month's breaks are
stored as reconciling items with a status, a history, and — once resolved — the
exact later transaction that resolved them. Three controls sit on top:

- **Rollforward** — open at start + raised − cleared − written off = open at end,
  checked against the items actually open, against the next period's opening
  figure, and against what the run recorded at the time.
- **Aging** — buckets as of period end with thresholds attached: an outstanding
  check past 60 days needs chasing and past 180 is stale-dated; unexplained
  money, or money the bank never saw, escalates at 30 days. Escalations are
  written to the item once, with an event, rather than recomputed each month.
- **Period locking** — closing a period activates triggers that reject new
  transactions, deletions, item restatements and run deletions. A closed
  period's items can still be *resolved* later, because a June check has to
  clear in July with June already closed. Reopening requires a reason and leaves
  a trail.

## Reading the report (9 tabs)

1. **Summary** — counts per pass, match rate, exception exposure, input
   completeness controls, and the reconciliation proof built from the *stated*
   bank and GL closing balances, tying to 0.00.
2. **Exceptions** — the working tab: ranked by exposure, colour-coded, with a
   probable cause, a suggested action and the source-file row.
3. **Matched — Exact / Timing / Tolerance** — the audit trail of every pair, with
   the method that made it, days delta, amount delta and fuzzy score.
4. **Open Items (aged)** — every item still open at this period's close,
   whichever period raised it, with age, bucket and flag.
5. **Rollforward** — the five-column control, with its check and any breaks.
6. **Bank Statement (source)** and **GL Extract (source)** — untouched inputs, so
   the workbook is self-contained for review.

Tabs 4 and 5 appear only in ledger mode.

## Using it with real data

Expected columns: `Date, Description, Reference, Amount` (bank),
`Date, Account, Memo, DocNo, Amount` (GL), and
`Source, PeriodStart, PeriodEnd, OpeningBalance, ClosingBalance` (balances — one
row each for `BANK` and `GL`, taken from the statement header and the trial
balance). Amounts are signed from the cash account's perspective (deposits +,
disbursements −). A QuickBooks *Transaction Detail by Account* export for the
cash account maps directly.

File names follow `bank_statement_<mon><year>.csv`, so `--period 2026-09` looks
for `sep2026`. Thresholds live at the top of `src/reconcile.py`
(`TIMING_WINDOW_DAYS`, `TOLERANCE_DOLLARS`, `TOLERANCE_WINDOW_DAYS`,
`FUZZY_THRESHOLD`, `MONTH_END_DAYS`, `CHECK_REF`) and `src/controls.py`
(`FOLLOW_UP_DAYS`, `ESCALATE_DAYS`, `STALE_DAYS`). Every run records the
thresholds it used.

## Design decisions worth knowing

- **Integer cents, parsed with `Decimal`.** Floats appear only when values are
  written to Excel. A matching engine that trusts `0.1 + 0.2 == 0.3` will
  eventually lie to you.
- **The proof is built on stated balances, not on the rows.** A proof computed
  from the rows ties by construction and can never fail. Delete one row from a
  file and this one breaks by exactly that amount.
- **Nothing disappears.** Pass-3 residual cents are a reconciling item, not a
  footnote — which is why a write-off booked next month clears them, and why an
  unbooked residual cannot silently leak into the following period.
- **Duplicates are identified by document, not by look-alike.** Same amount and
  same document number means duplicate; same vendor and amount with a different
  document is a recurring payment. The dataset contains three identical lease
  payments specifically to test this.
- **The accounting rules live in constraints.** One-to-one matching is a
  `UNIQUE`; an item may only be `CLEARED` if it records where it cleared; closed
  periods are locked by triggers. These hold against a future version of this
  code, not only against the engine.
- **Every rule has a planted test case.** The dataset contains a check that
  clears 11 days late, same-amount payments that defeat greedy matching,
  interest charged rather than earned, an NSF return that is not a fee, a
  deposit the bank never received, and items that clear one and two months
  later.

## Tests

104 tests: the engine's golden numbers and controls (25), the ledger's loading
and constraints (15), persistence (16), carry-forward across three months (19),
and the month-over-month controls (27). Key behaviours are mutation-tested — the
bug is reintroduced and the named test has to fail.

See [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) for the accounting logic and
interview-ready talking points, [docs/SCHEMA.md](docs/SCHEMA.md) for the ledger,
and [docs/THE_NSF_BUG.md](docs/THE_NSF_BUG.md) for the bug this project's own
test data caught.