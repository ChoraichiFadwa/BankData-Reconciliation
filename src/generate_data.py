"""
generate_data.py — Synthetic dataset generator for the Bank-to-GL Reconciliation engine.


Produces two CSVs for June 2026 (a realistic month-end close scenario for a
telecom-infrastructure contractor):

  data/bank_statement_jun2026.csv  — what the bank says happened (122 rows)
  data/gl_cash_extract_jun2026.csv — what the GL / QuickBooks cash account says (122 rows)

The two files deliberately disagree in the ways real books disagree:
  * 95 transactions match exactly (same amount, same date)
  * 12 transactions match on amount but clear the bank 1-4 days late (timing)
  *  8 transactions match within an amount tolerance + fuzzy description
       (rounding, FX cents, keyed-in-cents errors)
  * 14 rows have NO counterpart and must surface as exceptions:
       - bank fees / interest / NSF the bookkeeper never booked
       - an unidentified bank debit and credit
       - outstanding checks issued near month end
       - deposits in transit recorded on the last GL day
       - one duplicate vendor-payment posting in the GL

Deterministic: fixed RNG seed, so the engine's output is reproducible
(115 reconciled, 14 exceptions) on every run.
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 42
random.seed(SEED)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

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
    """Random amount with unique cents so amount-only matching is unambiguous."""
    while True:
        amt = round(random.uniform(lo, hi), 2)
        # avoid .00 endings so nothing collides with round fee amounts
        if int(round(amt * 100)) % 100 in (0, 50):
            continue
        if amt not in used_amounts:
            used_amounts.add(amt)
            return amt


def biz_day(d):
    """Roll weekend dates forward to Monday."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def rand_june_day(lo=1, hi=26):
    return biz_day(date(2026, 6, random.randint(lo, hi)))


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
        gl_rows.append((d, memo, ref, "1010 Cash — Operating", amt))
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
        gl_rows.append((d, f"{vendor} — invoice payment", doc, "1010 Cash — Operating", amt))

# ---------------------------------------------------------------- 12 timing matches
# GL books the payment/deposit on day X; bank clears it 1-4 business days later.
for i in range(12):
    vendor, bank_desc = random.choice(VENDORS)
    amt = -unique_amount(400, 18_000)
    gl_d = rand_june_day(2, 22)
    bank_d = biz_day(gl_d + timedelta(days=random.randint(1, 4)))
    doc = next_check()
    bank_rows.append((bank_d, f"CHEQUE {doc.replace('CHQ#', '')}", doc, amt))
    gl_rows.append((gl_d, f"{vendor} — invoice payment", doc, "1010 Cash — Operating", amt))

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
                    f"{vendor} — invoice payment", ref, "1010 Cash — Operating", -gl_amt))

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
gl_rows.extend((d, memo, doc, "1010 Cash — Operating", amt) for d, memo, doc, amt in gl_only)

# duplicate posting: clone an existing matched vendor payment (same doc, same amount)
dup_source = next(r for r in gl_rows if r[4] < -5_000 and "invoice payment" in r[1])
gl_rows.append((dup_source[0], dup_source[1], dup_source[2], dup_source[3], dup_source[4]))

# ---------------------------------------------------------------- write CSVs
DATA_DIR.mkdir(exist_ok=True)

bank_rows.sort(key=lambda r: (r[0], r[1]))
gl_rows.sort(key=lambda r: (r[0], r[1]))

with open(DATA_DIR / "bank_statement_jun2026.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Description", "Reference", "Amount"])
    for d, desc, ref, amt in bank_rows:
        w.writerow([d.isoformat(), desc, ref, f"{amt:.2f}"])

with open(DATA_DIR / "gl_cash_extract_jun2026.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Account", "Memo", "DocNo", "Amount"])
    for d, memo, doc, acct, amt in gl_rows:
        w.writerow([d.isoformat(), acct, memo, doc, f"{amt:.2f}"])

print(f"bank rows: {len(bank_rows)}  |  gl rows: {len(gl_rows)}")
print(f"wrote {DATA_DIR / 'bank_statement_jun2026.csv'}")
print(f"wrote {DATA_DIR / 'gl_cash_extract_jun2026.csv'}")
