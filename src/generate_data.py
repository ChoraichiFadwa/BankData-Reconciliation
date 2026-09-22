"""
generate_data.py — Synthetic dataset generator for the Bank-to-GL Reconciliation engine.

Produces three CSVs for June 2026 (a realistic month-end close scenario for a
telecom-infrastructure contractor):

  data/bank_statement_jun2026.csv  — what the bank says happened (127 rows)
  data/gl_cash_extract_jun2026.csv — what the GL / QuickBooks cash account says (127 rows)
  data/balances_jun2026.csv        — stated opening/closing balances: the bank
                                     statement header and the GL trial balance

The two transaction files deliberately disagree in the ways real books disagree:
  * 95 transactions match exactly (same amount, same date)
  * 15 transactions match on amount but clear the bank later (timing):
       - 13 checks, matched on check number (one clears 11 days late)
       -  2 recurring lease payments of the SAME amount, matched on date
  *  8 transactions match within an amount tolerance + fuzzy description
       (rounding, FX cents, keyed-in-cents errors)
  * 18 rows have NO counterpart and must surface as exceptions:
       - bank fees and interest the bookkeeper never booked
       - interest CHARGED on the line of credit (a debit, not income)
       - a customer cheque returned NSF (a reversed deposit, not a fee)
       - an unidentified bank debit and credit
       - outstanding checks issued near month end
       - a recurring payment initiated near month end, not yet cleared
       - deposits in transit recorded on the last GL day
       - a mid-month GL deposit the bank never received
       - one duplicate vendor-payment posting in the GL

The edge cases are appended after every random draw of the base scenario, so
the original 244 rows are unchanged. Each one exists to exercise one specific
engine rule; see the comments in the EDGE CASES section.

Deterministic: fixed RNG seed, so the engine's output is reproducible
(118 reconciled, 18 exceptions, proof ties to 0.00) on every run.
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 42
random.seed(SEED)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ACCT = "1010 Cash — Operating"
PERIOD_START = date(2026, 6, 1)
PERIOD_END = date(2026, 6, 30)
OPENING_BALANCE = 184_352.19   # first period: bank and GL opened in agreement

VENDORS = [
    ("Bell Canada", "EFT BELL CDA PAYMENT"),
    ("Rogers Communications", "EFT ROGERS COMM"),
    ("Telecon Design", "EFT TELECON DESIGN"),
    ("United Rentals", "PAD UNITED RENTALS"),
    ("Brandt Tractor Ltd", "EFT BRANDT TRACTOR"),
    ("WESCO Distribution", "EFT WESCO DIST"),
    ("Anixter Canada", "EFT ANIXTER CDA"),
    ("Shell Fleet Card", "PAD SHELL FLEET"),
    ("Petro-Canada SuperPass", "PAD PETROCAN SUPERPASS"),
    ("Enterprise Fleet Mgmt", "PAD ENTERPRISE FLEET"),
    ("WSIB Ontario", "EFT WSIB ONT PREMIUM"),
    ("Intact Insurance", "PAD INTACT INS"),
    ("Staples Business", "POS STAPLES BUS ADV"),
    ("Home Depot Pro", "POS HOME DEPOT PRO"),
    ("Vermeer Canada", "EFT VERMEER CDA"),
    ("Ditch Witch of Ontario", "CHQ DITCH WITCH ONT"),
    ("GFL Environmental", "PAD GFL ENVIRONMENTAL"),
    ("Milton Hydro", "PAD MILTON HYDRO"),
    ("Region of Halton", "EFT REGION HALTON PERMIT"),
    ("ADP Payroll", "ADP PAYROLL PPD"),
]

CUSTOMER_DEPOSITS = [
    ("Rogers Communications — progress billing", "DEP ROGERS COMM AP"),
    ("Telus network build — milestone", "DEP TELUS COMM AP"),
    ("Bell aerial build — holdback release", "DEP BELL CDA AP"),
    ("Cogeco underground build", "DEP COGECO CONNEXION"),
]

used_amounts = set()


def unique_amount(lo, hi):
    """Random amount with unique cents, so the base scenario is unambiguous.
    Ambiguity (same-amount payments) is introduced on purpose in EDGE CASES."""
    while True:
        amt = round(random.uniform(lo, hi), 2)
        # avoid .00 endings so nothing collides with round fee amounts
        if int(round(amt * 100)) % 100 in (0, 50):
            continue
        if amt not in used_amounts:
            used_amounts.add(amt)
            return amt


def reserve(amt):
    """Claim a hand-picked amount, failing loudly if the random draws already used it."""
    assert amt not in used_amounts, f"amount {amt} collides with a generated amount"
    used_amounts.add(amt)
    return amt


def biz_day(d):
    """Roll weekend dates forward to Monday."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def rand_june_day(lo=1, hi=26):
    return biz_day(date(2026, 6, random.randint(lo, hi)))


def cents(x):
    return int(round(x * 100))


bank_rows = []  # (date, description, ref, signed_amount)
gl_rows = []    # (date, memo, doc_no, account, signed_amount)

check_no = 1041
eft_ref = 40113


def next_check():
    global check_no
    check_no += 1
    return f"CHQ#{check_no}"


def next_ref():
    global eft_ref
    eft_ref += random.randint(3, 19)
    return f"REF{eft_ref}"


# ---------------------------------------------------------------- 95 exact matches
for i in range(95):
    if i % 5 == 0:  # every 5th is a customer deposit (money in)
        memo, bank_desc = random.choice(CUSTOMER_DEPOSITS)
        amt = unique_amount(25_000, 115_000)
        d = rand_june_day()
        ref = next_ref()
        bank_rows.append((d, f"{bank_desc} {ref}", ref, amt))
        gl_rows.append((d, memo, ref, ACCT, amt))
    else:  # vendor disbursement (money out)
        vendor, bank_desc = random.choice(VENDORS)
        amt = -unique_amount(180, 24_000)
        d = rand_june_day()
        if random.random() < 0.25:
            doc = next_check()
            bank_rows.append((d, f"CHEQUE {doc.replace('CHQ#', '')}", doc, amt))
        else:
            doc = next_ref()
            bank_rows.append((d, f"{bank_desc} {doc}", doc, amt))
        gl_rows.append((d, f"{vendor} — invoice payment", doc, ACCT, amt))

# ---------------------------------------------------------------- 12 timing matches
# GL books the check on day X; the bank clears it 1-4 business days later.
for i in range(12):
    vendor, bank_desc = random.choice(VENDORS)
    amt = -unique_amount(400, 18_000)
    gl_d = rand_june_day(2, 22)
    bank_d = biz_day(gl_d + timedelta(days=random.randint(1, 4)))
    doc = next_check()
    bank_rows.append((bank_d, f"CHEQUE {doc.replace('CHQ#', '')}", doc, amt))
    gl_rows.append((gl_d, f"{vendor} — invoice payment", doc, ACCT, amt))

# ---------------------------------------------------------------- 8 tolerance + fuzzy matches
# Amounts differ by a few cents (rounding / keying), descriptions only resemble.
fuzzy_specs = [
    ("Bell Canada", "EFT BELL CDA PAYMENT", 4_183.67, -0.09),
    ("Shell Fleet Card", "PAD SHELL FLEET CARD SVC", 2_411.28, 0.18),
    ("WESCO Distribution", "EFT WESCO DIST CANADA", 13_902.44, -0.36),
    ("United Rentals", "PAD UNITED RENTALS INC", 6_648.91, 0.27),
    ("Intact Insurance", "PAD INTACT INSURANCE PREM", 1_887.53, -0.45),
    ("Anixter Canada", "EFT ANIXTER CDA SUPPLY", 9_274.16, 0.63),
    ("Milton Hydro", "PAD MILTON HYDRO UTIL", 1_154.82, -0.14),
    ("GFL Environmental", "PAD GFL ENVIRONMENTAL SVC", 743.29, 0.31),
]
for vendor, bank_desc, gl_amt, cents_off in fuzzy_specs:
    d = rand_june_day(3, 24)
    bank_amt = round(-(gl_amt + cents_off), 2)
    used_amounts.add(gl_amt)
    used_amounts.add(abs(bank_amt))
    ref = next_ref()
    bank_rows.append((d, f"{bank_desc} {ref}", ref, bank_amt))
    gl_rows.append((biz_day(d + timedelta(days=random.randint(0, 2))),
                    f"{vendor} — invoice payment", ref, ACCT, -gl_amt))

# ---------------------------------------------------------------- 7 bank-only exceptions
bank_only = [
    (date(2026, 6, 30), "MONTHLY ACCOUNT FEE", "SVC-JUN", -125.00),
    (date(2026, 6, 30), "SERVICE CHARGE - WIRE PAYMENT", "WIRE-FEE", -45.00),
    (date(2026, 6, 30), "INTEREST EARNED", "INT-JUN", 214.87),
    (date(2026, 6, 18), "NSF RETURNED ITEM FEE", "NSF-0618", -48.00),
    (date(2026, 6, 22), "MERCHANT PROCESSING FEE MONERIS", "MER-0622", -389.44),
    (date(2026, 6, 25), "PRE-AUTH DEBIT UNKNOWN ORIG 7719", "PAD-7719", -1_260.00),
    (date(2026, 6, 26), "E-TRANSFER RECEIVED T4X99A", "ETR-T4X99A", 2_150.00),
]
bank_rows.extend((d, desc, ref, amt) for d, desc, ref, amt in bank_only)

# ---------------------------------------------------------------- 7 GL-only exceptions
gl_only = [
    # outstanding checks — issued late June, not yet cleared
    (date(2026, 6, 26), "Ditch Witch of Ontario — parts invoice", next_check(), -7_412.66),
    (date(2026, 6, 29), "Vermeer Canada — drill head rebuild", next_check(), -15_890.23),
    (date(2026, 6, 29), "Region of Halton — road permit fees", next_check(), -2_340.55),
    (date(2026, 6, 30), "Brandt Tractor Ltd — service invoice", next_check(), -4_178.09),
    # deposits in transit — booked on the last GL day, hit the bank in July
    (date(2026, 6, 30), "Rogers Communications — progress billing", next_ref(), 48_310.77),
    (date(2026, 6, 30), "Cogeco underground build", next_ref(), 12_764.31),
]
gl_rows.extend((d, memo, doc, ACCT, amt) for d, memo, doc, amt in gl_only)

# duplicate posting: clone an existing matched vendor payment (same doc, same amount)
dup_source = next(r for r in gl_rows if r[4] < -5_000 and "invoice payment" in r[1])
gl_rows.append((dup_source[0], dup_source[1], dup_source[2], dup_source[3], dup_source[4]))

# ---------------------------------------------------------------- EDGE CASES
# (a) A check that takes 11 days to clear. Outside the ±5-day window, so only
#     check-number matching can pair it; without it, it becomes two false
#     exceptions (an "outstanding check" and an "unidentified debit").
late_chk = next_check()
late_amt = reserve(5_612.38)
gl_rows.append((date(2026, 6, 5), "Telecon Design — survey invoice", late_chk, ACCT, -late_amt))
bank_rows.append((date(2026, 6, 16), f"CHEQUE {late_chk.replace('CHQ#', '')}", late_chk, -late_amt))

# (b) Three vehicle leases at the SAME amount, each with its own EFT reference.
#     Units 1-2 test optimal timing assignment: greedy nearest-date pairs the
#     Jun 10 debit with the Jun 11 booking, stranding the Jun 15 debit 7 days
#     from the Jun 8 booking. Unit 3 (booked Jun 29, not yet debited) tests that
#     a recurring payment is not mistaken for a duplicate posting.
lease_amt = reserve(1_286.43)
for unit, gl_d, bank_d in [(1, date(2026, 6, 8), date(2026, 6, 10)),
                           (2, date(2026, 6, 11), date(2026, 6, 15)),
                           (3, date(2026, 6, 29), None)]:
    ref = next_ref()
    gl_rows.append((gl_d, f"Enterprise Fleet Mgmt — vehicle lease unit {unit}", ref, ACCT, -lease_amt))
    if bank_d:
        bank_rows.append((bank_d, f"PAD ENTERPRISE FLEET {ref}", ref, -lease_amt))

# (c) Interest CHARGED on the operating line: a debit. Must not be booked as income.
bank_rows.append((date(2026, 6, 30), "INTEREST ON OPERATING LINE OF CREDIT", "LOC-INT-JUN", -612.08))

# (d) A customer cheque returned NSF: reverses a deposit (DR A/R), it is not a fee.
bank_rows.append((date(2026, 6, 19), "RETURNED ITEM NSF CUSTOMER CHQ 5521", "RET-0619", -3_875.40))

# (e) A mid-month deposit booked in the GL that never reached the bank.
#     Not "in transit" — deposits clear in a day or two.
gl_rows.append((date(2026, 6, 12), "Cogeco underground build — change order", next_ref(), ACCT,
                reserve(9_406.58)))

# ---------------------------------------------------------------- stated balances
# The engine must NOT derive these from the rows: they are the independent
# figures (statement header, trial balance) the reconciliation proves against.
open_c = cents(OPENING_BALANCE)
bank_close_c = open_c + sum(cents(r[3]) for r in bank_rows)
gl_close_c = open_c + sum(cents(r[4]) for r in gl_rows)

# ---------------------------------------------------------------- write CSVs
DATA_DIR.mkdir(parents=True, exist_ok=True)

bank_rows.sort(key=lambda r: (r[0], r[1]))
gl_rows.sort(key=lambda r: (r[0], r[1]))

with open(DATA_DIR / "bank_statement_jun2026.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Description", "Reference", "Amount"])
    for d, desc, ref, amt in bank_rows:
        w.writerow([d.isoformat(), desc, ref, f"{amt:.2f}"])

with open(DATA_DIR / "gl_cash_extract_jun2026.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Account", "Memo", "DocNo", "Amount"])
    for d, memo, doc, acct, amt in gl_rows:
        w.writerow([d.isoformat(), acct, memo, doc, f"{amt:.2f}"])

with open(DATA_DIR / "balances_jun2026.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Source", "PeriodStart", "PeriodEnd", "OpeningBalance", "ClosingBalance"])
    for src, close_c in (("BANK", bank_close_c), ("GL", gl_close_c)):
        w.writerow([src, PERIOD_START.isoformat(), PERIOD_END.isoformat(),
                    f"{open_c / 100:.2f}", f"{close_c / 100:.2f}"])

print(f"bank rows: {len(bank_rows)}  |  gl rows: {len(gl_rows)}")
print(f"wrote {DATA_DIR / 'bank_statement_jun2026.csv'}")
print(f"wrote {DATA_DIR / 'gl_cash_extract_jun2026.csv'}")
print(f"wrote {DATA_DIR / 'balances_jun2026.csv'}")