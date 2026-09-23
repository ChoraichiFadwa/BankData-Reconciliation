# Bank-to-GL Reconciliation — design notes

Every step of the project: what was built, why, how it serves the use case,
which choices were made and what was rejected, and what is still missing.

---

## 0. The use case

At every month-end close, two records of the same bank account have to be
proved against each other: the bank statement, and the cash account in the
general ledger. They never agree on the first pass, for legitimate reasons
(a check written but not yet cashed, a deposit booked on the 30th that the bank
credits on the 2nd) and for bad ones (a fee nobody booked, an invoice paid
twice, a debit nobody recognises).

The job is to pair up everything that is genuinely the same transaction, and to
explain every remaining dollar. Done by hand on ~120 transactions it is an
afternoon of tick-and-tie, and the usual output is one undifferentiated
"unmatched" list that still needs triage.

Two things follow, and they shape the whole project:

1. **The valuable output is the exception list, not the match count.** Matching
   is the part that needs no judgment; the breaks are where a person's time
   should go. So each break has to arrive ranked by dollar exposure, diagnosed
   with a probable cause, and paired with the journal entry or follow-up that
   clears it.
2. **A reconciliation is not a monthly one-off; it is a continuous control.**
   June's items are not results, they are open items with a future. Most clear
   in July. One check won't clear for months and has to age. One deposit may
   never clear and has to be written off. Everything in phases 1–4 below exists
   because of this second point.

---

## 1. Version one: the stateless engine

Two CSVs in, an Excel report out. Three matching passes of decreasing
strictness, then classification of whatever survives.

- **Pass 1 — exact:** same signed amount, same date.
- **Pass 2 — timing:** same amount, cleared a few days later.
- **Pass 3 — tolerance + fuzzy:** amounts within $0.99, dates within 7 days, and
  descriptions that actually resemble each other.

**Why strictest-first:** each pass only sees what earlier passes left. If a
sloppy tolerance match ran first it could steal a row that had an exact partner,
and that partner would then surface as a fake exception. Strictest-first makes
the result stable and defensible in review.

**Why fuzzy matching is gated on description:** amount-only tolerance matching is
how reconciliation tools invent false matches. Two unrelated vendors 40¢ apart
must not pair.

**Why one-to-one:** each bank row consumes at most one GL row. This is what
forces a duplicate GL posting into the open — the bank has one payment, the GL
has two identical entries, so exactly one is left standing.

Reported result: 115 reconciled, 14 exceptions, proof $0.00.

---

## 2. Phase 0 — auditing the engine before trusting it

A regression test freezes behaviour, so the behaviour has to be right first.
Reviewing the code found bugs that changed the numbers.

### 2.1 The proof could not fail

Both balances were computed as `opening + sum(rows)`. Every unmatched row was
then added to one side or the other, so the difference cancelled to zero **for
any input whatsoever**. The report's headline control was circular.

*Fix:* the proof starts from **stated** balances — the bank statement header and
the GL trial balance — written by the generator to a third CSV. Two completeness
controls check that each file's rows actually roll its stated opening balance to
its stated closing balance.

*Why it matters to the use case:* the point of a rec is to prove completeness. A
truncated export or a filtered report is a real failure mode, and the old proof
would have reported 0.00 anyway. Deleting one row now breaks the proof by
exactly that row's amount.

### 2.2 Four wrong diagnoses

Each of these would have told an accountant to book the wrong entry:

| Bug | Wrong output | Fix |
|---|---|---|
| Interest ignored the sign | Interest *charged* booked as income | Sign decides: credit → income, debit → expense |
| NSF matched the fee pattern | A $3,875 returned cheque booked as a bank charge | A returned item reverses a receivable; only the separate fee is a charge |
| Duplicates found by look-alike | Recurring payments flagged "reverse the duplicate JE" | Same amount **and same document number** means duplicate; different document means recurring |
| Every GL-only deposit called "in transit" | A deposit missing for 18 days looked routine | Near month end → in transit; mid-period → the bank never received it, investigate |

### 2.3 Two matching gaps

- **Checks clearing late.** A check written June 5 and cleared June 16 fell
  outside the ±5-day window and became two false exceptions. *Fix:* match checks
  on check number regardless of lag — the bank echoes the number, which is
  stronger evidence than a date window.
- **Greedy nearest-date.** With several same-amount rows competing, an early
  bank row took the GL row that a later one needed. *Fix:* solve the assignment
  optimally per amount group (most pairs, then least total lag). A global sort by
  date gap was tried first and also fails, which is why the code uses a small DP
  instead.

### 2.4 Floats had crept back in

The README claimed integer cents, but the tolerance, the residuals and the proof
totals were floats, and `int(0.29 * 100)` truncates to 28. *Fix:* parse amounts
from the CSV text with `Decimal` into integer cents; floats appear only when
values are written to Excel.

### 2.5 Testing the fixes

Five edge cases were added to the generator, one per bug. Run against the
**original** engine they produced four false exceptions and seven wrong
diagnoses — which is how we know the bugs were real rather than theoretical.

New baseline: **118 reconciled, 18 exceptions** (later 19, see §6.3).

---

## 3. Phase 0b — the regression suite

27 tests in three layers, so a failure says *what* broke:

1. **Golden numbers** — counts per pass, and every exception with its category.
2. **One named test per edge case** — `test_late_check_matched_by_number`,
   `test_recurring_payment_not_flagged_duplicate`, and so on.
3. **Controls** — delete a row and the proof must break by that amount; change a
   stated balance by a cent and it must break by a cent; a category with no
   proof line must raise; the generator is deterministic and the committed CSVs
   match it; the files are valid UTF-8.

**Mutation-tested:** three bugs were deliberately reintroduced and each was
caught. Layer 3 earns its place — with correct data, the old circular proof
still shows 0.00, so only the controls expose it.

---

## 4. Why a ledger, and why SQL

The engine had no memory: everything it learned died when the process exited.
That is fine for one month and wrong for a close. Questions it could not answer:

- What was still open at the end of June?
- Did this July bank row clear a June item, or is it new?
- How long has this check been outstanding?
- Did anything get lost between June's closing items and July's opening items?
- What was run for June, with which thresholds, and has anyone changed it since?

Each is a question about state that survives between runs.

**Alternatives considered:**

- *More CSVs or a JSON file of open items.* Works as storage; has no constraints
  and no queries. Nothing stops an item being written twice or a "cleared" item
  pointing nowhere, and every question becomes a Python loop. It ends up as a
  weak database.
- *Excel as the store.* Same, plus hand-editable — fatal for an audit trail.
- *Parquet / dataframes.* Good for analytics; no uniqueness, no foreign keys, no
  in-place status update, no transactions.
- *A document store.* The data is relational — matches reference two
  transactions, items reference a period and a run. Joins are the main
  operation.
- *Postgres.* Right for a real deployment, wrong for a portfolio repo: a server
  and credentials mean a reviewer can no longer clone and run. SQLite is one
  file, in the standard library, and the schema ports with small changes.

**What SQL gives concretely:** constraints as guarantees (one-to-one matching is
a `UNIQUE`, not a Python convention); transactions, so a run lands whole or not
at all; declarative queries for pass 0, aging and the rollforward; triggers for
period locking. And it is the skill most screened for in the roles this project
targets.

---

## 5. Phase 1 — the ledger

`sql/schema.sql` (seven tables) and `src/db.py` (create, connect, load).

- **`recon_period`** — each month with its four stated balances and an
  `OPEN`/`CLOSED` status.
- **`bank_txn` / `gl_entry`** — the source rows, verbatim.
- **`recon_run`** — one row per execution: thresholds used, counts, proof
  difference, completeness figures.
- **`match`** — each pair, with the pass and method that made it.
- **`reconciling_item`** — an exception as a living object with a status.
- **`item_event`** — append-only history.

**Choices:**

- *Row identity includes the line number.* Hashing content alone would treat the
  planted duplicate — identical to another GL line in every field — as a reload
  and silently drop it. That is the exact break the engine exists to catch.
- *Changed data is refused, not merged.* Reloading an identical file is a no-op;
  reloading a changed file requires `--replace`. Silently keeping stale rows
  would poison every number built on them.
- *Accounting rules live in constraints.* `UNIQUE (run_id, bank_txn_id)` is the
  one-to-one rule; an item may only be `CLEARED` if it records where it cleared,
  which is what stops the rollforward drifting.
- *Foreign keys are enabled per connection*, since SQLite defaults them off —
  verified by a test that fails when the pragma is removed.

Engine behaviour unchanged; the 27 tests still passed untouched.

---

## 6. Phase 2 — wiring the engine to the ledger

After phase 1 the store existed but nothing wrote to it. Phase 2 connects both
directions, with no behaviour change: same 95 / 15 / 8, same proof.

**Reading:** `reconcile()` accepts either three file paths or a connection plus a
period id. A parity test pins that both produce identical matches, exceptions
and proof. File mode is kept so the repo still runs with no database.

**Writing:** `store.save_run()` persists the run, every match, every exception as
an `OPEN` item, and a `CREATED` event each — in one transaction.

**Choices:**

- *Rerunning supersedes.* One run per period, never two sets of items. `run_id`
  is `AUTOINCREMENT`, so a superseded number is never reused: "run 3" means one
  thing forever.
- *Saving requires ledger ids.* CSV frames have none, so `save_run` refuses
  rather than guessing which row is which.
- *All or nothing.* A simulated mid-write failure leaves zero runs, zero matches,
  zero items. A half-written month would corrupt every later rollforward.

---

## 7. Phase 3 — more than one month

A ledger holding one month proves nothing: items can be raised but never
resolved. The generator now builds three chained months, each opening where the
last closed, **per side**.

From July on, the bank and the GL open at *different* balances — July's gap is
$33,133.92 — because June's items are still outstanding. Explaining that gap is
the whole job of pass 0 and the rollforward.

**What the months contain:** June's items mostly resolve in July (three of four
checks clear, both deposits are credited, the bookkeeper posts the seven JEs the
report asked for, the duplicate is reversed, the residual is written off).
Others deliberately do not: the Vermeer check is still outstanding at the end of
August (63 days), the deposit the bank never received is 80 days old, and two
unidentified June items were never traced. July raises its own items, which
clear in August in turn.

**Choice:** June's rows are byte-identical to the single-month version — July and
August draw from the RNG only after June finishes — so the original tests and the
committed June data stand unchanged.

**The control experiment:** before building pass 0, July was reconciled with the
existing engine. Result: 19 exceptions instead of 10, fourteen of them false
(June's cleared items reappearing as "unidentified" debits and credits), and the
proof off by exactly the opening gap. That is now a permanent test,
`test_july_without_pass_0_is_wrong` — the executable answer to "why does this
need a database?"

---

## 8. Phase 4 — pass 0, the carry-forward pass

`sql/pass0_carryforward.sql`. Unlike every other pass, its input is not a file;
it is the ledger. It asks what is still open from earlier periods and finds this
period's transactions that resolve those items.

**The four resolution rules:**

1. a GL-side item (outstanding check, payment or deposit in transit) clears when
   the **bank** finally shows it — same amount, and same check number when there
   is one;
2. a bank-side item (charge, interest, NSF return, unidentified) clears when the
   bookkeeper **books** it;
3. a duplicate posting clears when it is **reversed** — same amount, opposite
   sign, same side;
4. the pass-3 residual clears when its **write-off JE** is booked.

**Choices:**

- *SQL returns candidates, Python decides.* One item can have several candidates
  and one transaction can suit several items. The join is declarative; the
  one-to-one assignment, strictest rule first (a check number beats a bare
  amount), is Python. Neither tool is forced to do the other's job.
- *Consumed rows are withheld from passes 1–3 but still counted in the
  completeness controls.* They belong to an earlier period's item, but they are
  genuinely rows of this month's file.
- *Resolution is recorded on the item, not as a match row.* A duplicate reversal
  pairs two GL rows and a residual has no source row at all, so `match` — which
  requires one row from each side — is the wrong home. Each item records the
  period, the run and the exact transaction that cleared it.
- *`carryforward.resolve()` decides and writes nothing;* `save_run()` persists
  inside the run's transaction, so a failed run leaves the ledger untouched.

**Two engine changes this forced:**

- **The pass-3 residual became a reconciling item.** As a proof footnote it meant
  the next period silently inherited the gap — July was off by June's $0.35. It
  is now an item of its own (`side = 'RESIDUAL'`, belonging to neither file) that
  stays on the rec until the write-off JE is booked. This is why June reports 19
  items rather than 18.
- **"Open" means open as of the *start* of the period.** Found by a test:
  rerunning July lost all 15 clearings, because pass 0 looked for items open
  *now* and its own previous run had already cleared them. It now also considers
  items resolved by this same period, which a rerun takes back.

**Results — three months closed in sequence, every proof at $0.00:**

| Period | Rows (bank/GL) | Pass 0 | Passes 1–3 | Items raised | Open at period end |
|---|---|---|---|---|---|
| 2026-06 | 127 / 127 | — | 95 / 15 / 8 | 19 | 19 |
| 2026-07 | 91 / 93 | 15 cleared | 70 / 8 / 4 | 6 | 10 |
| 2026-08 | 60 / 62 | 5 cleared | 50 / 5 / 2 | 4 | 9 |

What remains open at the end of August is what should: the June check (63 days),
the deposit never received (80 days), two untraced June items (66–67 days),
July's unidentified debit (38 days), and August's own fresh items.

`store.write_off()` closes an item by decision rather than by clearing — the
accountant's call, recorded with its own event.

---

## 9. Where the project stands

- **77 tests:** 27 engine, 15 ledger, 16 store, 19 carry-forward. Key behaviours
  mutation-tested — reintroduce the bug, watch the named test fail.
- **One command per month:** `python src/reconcile.py --period 2026-07` loads,
  carries forward, reconciles, reports and saves. Each month writes its own
  report file.
- **File mode still works with no database** — and is the control that shows what
  the memory buys.

**Principles that recur through every phase:** money is integer cents; nothing
disappears silently; the database enforces what the accounting requires; a
control that cannot fail is not a control; and every rule the engine relies on
has a planted test case in the data.

---

## 10. Still to build

**Phase 5 — the controls that make the ledger pay off.**

- *Rollforward:* opening open items + new − cleared − written off = closing open
  items. It must balance every month, and June's closing items must equal July's
  opening items. This is the control a reviewer actually asks for, and it is one
  query.
- *Aging:* buckets (0–30 / 31–60 / 61–90 / 90+) as of period end, with
  stale-check and escalation flags. Aging is currently computed and displayed
  but has no thresholds attached.
- *Period locking:* a trigger that rejects any write to a closed period, so
  closed books stay closed — including against a future version of this code.

**Phase 6 — surfacing it.** Two report tabs (Open Items aged, Rollforward) and a
`close --period` command. Right now the SQL work is invisible to anyone who only
opens the Excel file.

**Phase 7 — documentation.** The README and walkthrough still describe the
single-month tool: they need the new numbers, the carry-forward pass, the
schema, and the "why SQL" argument.

**Deliberately out of scope:** many-to-one matching for batched deposits (three
GL receipts settling as one bank credit), and a per-bank/ERP configuration file
for different export formats.