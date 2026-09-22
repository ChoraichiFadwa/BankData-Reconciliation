# The NSF Bug

*The story of the one bug in this project, how my own test data caught it, and why
it made the engine better. Told the way I'd tell it in an interview.*

---

## The setup

When I built the exception classifier — the part of the engine that looks at an
unmatched bank transaction and diagnoses it — I gave it a simple rule for spotting
bank charges. If the description contains a fee-ish word, it's a fee:

```python
FEE_PATTERN = re.compile(r"FEE|SERVICE CHARGE|SVC CHG|NSF|OVERDRAFT", re.I)
```

Monthly account fee? Matches `FEE`. NSF returned item? Matches `NSF`. Wire service
charge? Matches `SERVICE CHARGE`. I ran it, watched twelve of the fourteen planted
exceptions come back correctly labeled, and felt pretty good about it.

## The catch

One of my planted exceptions was an incoming Interac e-transfer — $2,150 that hit
the bank account with no matching entry in the books. The right diagnosis is
*"unidentified bank credit — go find out who sent us money before you book it."*

The engine's diagnosis:

> **E-TRANSFER RECEIVED T4X99A** → *Bank charge — not booked in GL.*
> *Suggested action: Book JE — DR 6220 Bank Charges / CR 1010 Cash.*

My classifier had looked at $2,150 of incoming money and confidently recommended
**debiting it to bank-charge expense**.

## The hunt

Nothing in "E-TRANSFER RECEIVED" says fee. No FEE, no SERVICE CHARGE, no OVERDRAFT.
I stared at the string for a while before I saw it:

```
E - T R A - N S F - E R
        ↑ ↑ ↑
```

**tra·NSF·er.** The letters N-S-F — the pattern I'd added to catch *non-sufficient
funds* fees — sit right in the middle of the word "transfer." My regex had no idea
about word boundaries; it was happy to find NSF anywhere, including inside one of
the most common words in banking. Every e-transfer, wire transfer, and transfer
between accounts would have been classified as an NSF fee.

## The fix

One character class, twice:

```python
# before
FEE_PATTERN = re.compile(r"FEE|SERVICE CHARGE|SVC CHG|NSF|OVERDRAFT", re.I)

# after
FEE_PATTERN = re.compile(r"\bFEE\b|SERVICE CHARGE|SVC CHG|\bNSF\b|OVERDRAFT", re.I)
```

`\b` is a word boundary — it tells the pattern that NSF only counts when it stands
alone as a word. "NSF RETURNED ITEM FEE" still matches. "E-TRANSFER" no longer does.
I gave `FEE` the same treatment while I was there, so a vendor named "Fee-land
Coffee" (or, more realistically, a payee like "McAfee") can never be booked to bank
charges either, and applied word boundaries to the interest pattern for the same
reason. Re-ran the engine: fourteen exceptions, fourteen correct diagnoses.

## Why I tell this story

**It's exactly why the synthetic dataset exists.** I didn't build the test data
because a tutorial said to — I built it so that every failure mode the engine claims
to handle has a planted, *known-answer* example. The e-transfer row existed
specifically to test the "unidentified credit" path, and it did its job: it caught
the bug on the first full run, before the tool ever touched anything that mattered.
If I'd tested only against fees and checks — the cases I'd designed the rules around
— the bug ships silently.

**It's a finance bug, not just a code bug.** The failure mode wasn't a crash. It was
a *confident, plausible, wrong* accounting recommendation: expense an unidentified
deposit. In a reconciliation tool, the dangerous bugs are the quiet ones that
misclassify — a crash gets fixed the same afternoon, a misclassification survives
until an auditor finds it. That's shaped how I think about any automation I build
for accounting work: the test isn't "does it run," the test is "does it give the
answer a careful accountant would give," checked line by line against ground truth.

**And it's a one-word moral:** every substring is innocent until proven a word.
Pattern-matching on financial text without word boundaries is how "transfer" becomes
an NSF fee — and the fix costs four characters, *if* your test data is good enough
to show you it's needed.

---

*The fixed pattern lives in [`src/reconcile.py`](../src/reconcile.py) — see
`FEE_PATTERN`. The e-transfer row that caught it is planted in
[`src/generate_data.py`](../src/generate_data.py) under the bank-only exceptions.*
