"""
generate_data.py — Synthetic dataset generator for the Bank-to-GL Reconciliation engine.

Builds three chained months for a telecom-infrastructure contractor (June,
July and August 2026), each with three CSVs:

  data/bank_statement_<tag>.csv   — what the bank says happened
  data/gl_cash_extract_<tag>.csv  — what the GL / QuickBooks cash account says
  data/balances_<tag>.csv         — stated opening/closing balances

The months are a close, not three independent files:

  * each period opens where the last one closed, per side. From July on the
    bank and the GL open at DIFFERENT balances, because June's reconciling
    items are still outstanding — which is exactly what the carry-forward
    pass and the rollforward control exist to handle.
  * most of June's 18 reconciling items resolve in July: outstanding checks
    clear at the bank, deposits in transit are credited, and the bookkeeper
    posts the bank charges, interest and the NSF return that were never
    booked. The duplicate posting is reversed.
  * some deliberately do NOT resolve: one check is still outstanding at the
    end of August (it ages past 60 days), the two unidentified bank items are
    never traced, and the deposit the bank never received stays open until
    somebody writes it off.
  * July raises its own items, and they clear in August in turn.

June's rows are byte-identical to the single-month version: July and August
draw from the RNG only after June is finished, so June's seed stream is
untouched and its regression test still stands.

Deterministic: fixed RNG seed.
"""

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 42
random.seed(SEED)

_ap = argparse.ArgumentParser(description="Generate the synthetic 2026 dataset")
_ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "data"),
                 help="where to write the CSVs (default: data/). Tests use a temp folder.")
DATA_DIR = Path(_ap.parse_args().out_dir)

ACCT = "1010 Cash — Operating"
OPENING_BALANCE = 184_352.19   # June opens with bank and GL in agreement

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
check_no = 1041
eft_ref = 40113
periods = {}          # period_id -> rows + calendar


# ------------------------------------------------------------------ helpers
def unique_amount(lo, hi):
    """Random amount with unique cents, so the base scenario is unambiguous.
    Ambiguity (same-amount payments) is introduced on purpose in EDGE CASES."""
    while True:
        amt = round(random.uniform(lo, hi), 2)
        if int(round(amt * 100)) % 100 in (0, 50):
            continue
        if amt not in used_amounts:
            used_amounts.add(amt)
            return amt


def reserve(amt):
    """Claim a hand-picked amount, failing loudly if a random draw already used it."""
    assert abs(amt) not in used_amounts, f"amount {amt} collides with a generated amount"
    used_amounts.add(abs(amt))
    return amt


def biz_day(d):
    """Roll weekend dates forward to Monday."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def next_check():
    global check_no
    check_no += 1
    return f"CHQ#{check_no}"


def next_ref():
    global eft_ref
    eft_ref += random.randint(3, 19)
    return f"REF{eft_ref}"


def cents(x):
    return int(round(x * 100))


def new_period(period_id, year, month, last_day):
    periods[period_id] = {"bank": [], "gl": [], "year": year, "month": month,
                          "start": date(year, month, 1), "end": date(year, month, last_day)}
    return periods[period_id]


def bank_row(p, d, desc, ref, amt):
    p["bank"].append((d, desc, ref, amt))


def gl_row(p, d, memo, doc, amt):
    p["gl"].append((d, memo, doc, ACCT, amt))


def rand_day(p, lo, hi):
    return biz_day(date(p["year"], p["month"], random.randint(lo, hi)))


def cheque_desc(chk):
    return f"CHEQUE {chk.replace('CHQ#', '')}"


# ------------------------------------------------------------------ base scenario
def base_scenario(p, n_exact, n_timing, fuzzy_specs, day_hi):
    """The bulk of a month: rows that reconcile. Exact pairs, checks that clear
    a few days late, and a handful of cent-level differences."""
    for i in range(n_exact):
        if i % 5 == 0:                                   # customer deposit (money in)
            memo, bank_desc = random.choice(CUSTOMER_DEPOSITS)
            amt = unique_amount(25_000, 115_000)
            d = rand_day(p, 1, day_hi)
            ref = next_ref()
            bank_row(p, d, f"{bank_desc} {ref}", ref, amt)
            gl_row(p, d, memo, ref, amt)
        else:                                            # vendor disbursement (money out)
            vendor, bank_desc = random.choice(VENDORS)
            amt = -unique_amount(180, 24_000)
            d = rand_day(p, 1, day_hi)
            if random.random() < 0.25:
                doc = next_check()
                bank_row(p, d, cheque_desc(doc), doc, amt)
            else:
                doc = next_ref()
                bank_row(p, d, f"{bank_desc} {doc}", doc, amt)
            gl_row(p, d, f"{vendor} — invoice payment", doc, amt)

    for _ in range(n_timing):                            # booked, clears 1-4 days later
        vendor, bank_desc = random.choice(VENDORS)
        amt = -unique_amount(400, 18_000)
        gl_d = rand_day(p, 2, day_hi - 4)
        bank_d = biz_day(gl_d + timedelta(days=random.randint(1, 4)))
        doc = next_check()
        bank_row(p, bank_d, cheque_desc(doc), doc, amt)
        gl_row(p, gl_d, f"{vendor} — invoice payment", doc, amt)

    for vendor, bank_desc, gl_amt, cents_off in fuzzy_specs:
        d = rand_day(p, 3, day_hi - 2)
        bank_amt = round(-(gl_amt + cents_off), 2)
        used_amounts.add(gl_amt)
        used_amounts.add(abs(bank_amt))
        ref = next_ref()
        bank_row(p, d, f"{bank_desc} {ref}", ref, bank_amt)
        gl_row(p, biz_day(d + timedelta(days=random.randint(0, 2))),
               f"{vendor} — invoice payment", ref, -gl_amt)


# ================================================================== JUNE 2026
# Byte-identical to the single-month version: same draws, same order.
jun = new_period("2026-06", 2026, 6, 30)

base_scenario(jun, 95, 12, [
    ("Bell Canada", "EFT BELL CDA PAYMENT", 4_183.67, -0.09),
    ("Shell Fleet Card", "PAD SHELL FLEET CARD SVC", 2_411.28, 0.18),
    ("WESCO Distribution", "EFT WESCO DIST CANADA", 13_902.44, -0.36),
    ("United Rentals", "PAD UNITED RENTALS INC", 6_648.91, 0.27),
    ("Intact Insurance", "PAD INTACT INSURANCE PREM", 1_887.53, -0.45),
    ("Anixter Canada", "EFT ANIXTER CDA SUPPLY", 9_274.16, 0.63),
    ("Milton Hydro", "PAD MILTON HYDRO UTIL", 1_154.82, -0.14),
    ("GFL Environmental", "PAD GFL ENVIRONMENTAL SVC", 743.29, 0.31),
], day_hi=26)

# --- 7 bank-only exceptions: the bank moved money the books never recorded
JUN_FEE_ACCOUNT, JUN_FEE_WIRE = -125.00, -45.00
JUN_INTEREST_EARNED, JUN_NSF_FEE = 214.87, -48.00
JUN_FEE_MERCHANT, JUN_UNID_DEBIT, JUN_UNID_CREDIT = -389.44, -1_260.00, 2_150.00
for d, desc, ref, amt in [
    (date(2026, 6, 30), "MONTHLY ACCOUNT FEE", "SVC-JUN", JUN_FEE_ACCOUNT),
    (date(2026, 6, 30), "SERVICE CHARGE - WIRE PAYMENT", "WIRE-FEE", JUN_FEE_WIRE),
    (date(2026, 6, 30), "INTEREST EARNED", "INT-JUN", JUN_INTEREST_EARNED),
    (date(2026, 6, 18), "NSF RETURNED ITEM FEE", "NSF-0618", JUN_NSF_FEE),
    (date(2026, 6, 22), "MERCHANT PROCESSING FEE MONERIS", "MER-0622", JUN_FEE_MERCHANT),
    (date(2026, 6, 25), "PRE-AUTH DEBIT UNKNOWN ORIG 7719", "PAD-7719", JUN_UNID_DEBIT),
    (date(2026, 6, 26), "E-TRANSFER RECEIVED T4X99A", "ETR-T4X99A", JUN_UNID_CREDIT),
]:
    bank_row(jun, d, desc, ref, amt)

# --- 7 GL-only exceptions. The check numbers and refs are captured, because
#     July's bank statement has to clear these exact items.
CHK_DITCH, CHK_VERMEER, CHK_HALTON, CHK_BRANDT = (next_check(), next_check(),
                                                  next_check(), next_check())
REF_DIT_ROGERS, REF_DIT_COGECO = next_ref(), next_ref()
OS_DITCH, OS_VERMEER, OS_HALTON, OS_BRANDT = -7_412.66, -15_890.23, -2_340.55, -4_178.09
DIT_ROGERS, DIT_COGECO = 48_310.77, 12_764.31
for d, memo, doc, amt in [
    (date(2026, 6, 26), "Ditch Witch of Ontario — parts invoice", CHK_DITCH, OS_DITCH),
    (date(2026, 6, 29), "Vermeer Canada — drill head rebuild", CHK_VERMEER, OS_VERMEER),
    (date(2026, 6, 29), "Region of Halton — road permit fees", CHK_HALTON, OS_HALTON),
    (date(2026, 6, 30), "Brandt Tractor Ltd — service invoice", CHK_BRANDT, OS_BRANDT),
    (date(2026, 6, 30), "Rogers Communications — progress billing", REF_DIT_ROGERS, DIT_ROGERS),
    (date(2026, 6, 30), "Cogeco underground build", REF_DIT_COGECO, DIT_COGECO),
]:
    gl_row(jun, d, memo, doc, amt)

# duplicate posting: clone an existing matched vendor payment (same doc, same amount)
dup_source = next(r for r in jun["gl"] if r[4] < -5_000 and "invoice payment" in r[1])
jun["gl"].append(dup_source)
DUP_MEMO, DUP_AMOUNT = dup_source[1], dup_source[4]

# --- EDGE CASES: one per engine rule (see docs/WALKTHROUGH.md §5)
# (a) a check that takes 11 days to clear — only the check number pairs it
late_chk, late_amt = next_check(), reserve(5_612.38)
gl_row(jun, date(2026, 6, 5), "Telecon Design — survey invoice", late_chk, -late_amt)
bank_row(jun, date(2026, 6, 16), cheque_desc(late_chk), late_chk, -late_amt)

# (b) three vehicle leases at the SAME amount: units 1-2 test optimal timing
#     assignment, unit 3 (booked Jun 29) tests duplicate vs. recurring payment
LEASE_AMOUNT = reserve(1_286.43)
LEASE_REFS = {}
for unit, gl_d, bank_d in [(1, date(2026, 6, 8), date(2026, 6, 10)),
                           (2, date(2026, 6, 11), date(2026, 6, 15)),
                           (3, date(2026, 6, 29), None)]:
    ref = LEASE_REFS[unit] = next_ref()
    gl_row(jun, gl_d, f"Enterprise Fleet Mgmt — vehicle lease unit {unit}", ref, -LEASE_AMOUNT)
    if bank_d:
        bank_row(jun, bank_d, f"PAD ENTERPRISE FLEET {ref}", ref, -LEASE_AMOUNT)

# (c) interest CHARGED on the operating line: a debit, never booked as income
JUN_INTEREST_CHARGED = -612.08
bank_row(jun, date(2026, 6, 30), "INTEREST ON OPERATING LINE OF CREDIT", "LOC-INT-JUN",
         JUN_INTEREST_CHARGED)

# (d) a customer cheque returned NSF: reverses a deposit (DR A/R), not a fee
JUN_NSF_RETURN = -3_875.40
bank_row(jun, date(2026, 6, 19), "RETURNED ITEM NSF CUSTOMER CHQ 5521", "RET-0619",
         JUN_NSF_RETURN)

# (e) a mid-month deposit booked in the GL that never reached the bank
STALE_DEPOSIT = reserve(9_406.58)
gl_row(jun, date(2026, 6, 12), "Cogeco underground build — change order", next_ref(),
       STALE_DEPOSIT)


# ================================================================== JULY 2026
# Most of June's items resolve here — this is what a real July statement and a
# real July GL contain as a consequence of June.
jul = new_period("2026-07", 2026, 7, 31)

base_scenario(jul, 70, 8, [
    ("WESCO Distribution", "EFT WESCO DIST CANADA", 8_431.55, -0.22),
    ("Shell Fleet Card", "PAD SHELL FLEET CARD SVC", 3_127.84, 0.41),
    ("Milton Hydro", "PAD MILTON HYDRO UTIL", 1_402.19, -0.17),
    ("United Rentals", "PAD UNITED RENTALS INC", 5_233.60, 0.29),
], day_hi=27)

# --- June's outstanding checks clear at the bank (Vermeer's does NOT: it ages)
for d, chk, amt in [(date(2026, 7, 2), CHK_DITCH, OS_DITCH),
                    (date(2026, 7, 8), CHK_HALTON, OS_HALTON),
                    (date(2026, 7, 20), CHK_BRANDT, OS_BRANDT)]:
    bank_row(jul, d, cheque_desc(chk), chk, amt)

# --- June's deposits in transit are credited, and the lease payment is debited
bank_row(jul, date(2026, 7, 2), f"DEP ROGERS COMM AP {REF_DIT_ROGERS}", REF_DIT_ROGERS, DIT_ROGERS)
bank_row(jul, date(2026, 7, 3), f"DEP COGECO CONNEXION {REF_DIT_COGECO}", REF_DIT_COGECO, DIT_COGECO)
bank_row(jul, date(2026, 7, 2), f"PAD ENTERPRISE FLEET {LEASE_REFS[3]}", LEASE_REFS[3],
         -LEASE_AMOUNT)

# --- the bookkeeper posts June's bank items (the JEs the exception report asked for)
for i, (memo, amt) in enumerate([
    ("June bank charges — monthly account fee", JUN_FEE_ACCOUNT),
    ("June bank charges — wire service charge", JUN_FEE_WIRE),
    ("June bank charges — NSF returned item fee", JUN_NSF_FEE),
    ("June bank charges — merchant processing", JUN_FEE_MERCHANT),
    ("June interest income per bank statement", JUN_INTEREST_EARNED),
    ("June interest on operating line of credit", JUN_INTEREST_CHARGED),
    ("NSF returned customer cheque 5521 — recharge to A/R", JUN_NSF_RETURN),
], 1):
    gl_row(jul, date(2026, 7, 1), memo, f"JE-2607-{i:02d}", amt)

# --- and reverses the duplicate posting
gl_row(jul, date(2026, 7, 1), f"Reversal of duplicate posting — {DUP_MEMO}", "JE-2607-08",
       -DUP_AMOUNT)

# --- and writes off the cents pass 3 could not pair away in June
gl_row(jul, date(2026, 7, 1), "Write-off of June reconciliation residual", "JE-2607-09", -0.35)

# --- July's own exceptions: 3 bank-only, 2 GL-only (both clear in August)
JUL_FEE_ACCOUNT, JUL_INTEREST_EARNED, JUL_UNID_DEBIT = -135.00, 198.42, -740.00
bank_row(jul, date(2026, 7, 31), "MONTHLY ACCOUNT FEE", "SVC-JUL", JUL_FEE_ACCOUNT)
bank_row(jul, date(2026, 7, 31), "INTEREST EARNED", "INT-JUL", JUL_INTEREST_EARNED)
bank_row(jul, date(2026, 7, 24), "PRE-AUTH DEBIT UNKNOWN ORIG 8841", "PAD-8841", JUL_UNID_DEBIT)

CHK_JUL_OS, REF_JUL_DIT = next_check(), next_ref()
OS_JUL, DIT_JUL = reserve(-6_120.44), reserve(33_410.92)
gl_row(jul, date(2026, 7, 30), "Vermeer Canada — parts and labour", CHK_JUL_OS, OS_JUL)
gl_row(jul, date(2026, 7, 31), "Telus network build — milestone", REF_JUL_DIT, DIT_JUL)


# ================================================================== AUGUST 2026
# July's items clear in turn. June's stragglers are still open and now ageing.
aug = new_period("2026-08", 2026, 8, 31)

base_scenario(aug, 50, 5, [
    ("Anixter Canada", "EFT ANIXTER CDA SUPPLY", 6_915.73, -0.38),
    ("GFL Environmental", "PAD GFL ENVIRONMENTAL SVC", 884.12, 0.24),
], day_hi=27)

# --- July's outstanding check clears and its deposit in transit is credited
bank_row(aug, date(2026, 8, 5), cheque_desc(CHK_JUL_OS), CHK_JUL_OS, OS_JUL)
bank_row(aug, date(2026, 8, 4), f"DEP TELUS COMM AP {REF_JUL_DIT}", REF_JUL_DIT, DIT_JUL)

# --- the bookkeeper posts July's bank items
gl_row(aug, date(2026, 8, 1), "July bank charges — monthly account fee", "JE-2608-01",
       JUL_FEE_ACCOUNT)
gl_row(aug, date(2026, 8, 1), "July interest income per bank statement", "JE-2608-02",
       JUL_INTEREST_EARNED)
gl_row(aug, date(2026, 8, 1), "Write-off of July reconciliation residual", "JE-2608-03", -0.31)

# --- August's own exceptions. June's Vermeer check, the two unidentified June
#     items and the stale June deposit are all still open: nothing here
#     resolves them, which is the point.
bank_row(aug, date(2026, 8, 31), "MONTHLY ACCOUNT FEE", "SVC-AUG", reserve(-142.50))
gl_row(aug, date(2026, 8, 28), "Brandt Tractor Ltd — equipment rebuild", next_check(),
       reserve(-3_050.77))
gl_row(aug, date(2026, 8, 31), "Bell aerial build — holdback release", next_ref(),
       reserve(21_880.15))


# ================================================================== write CSVs
MONTH_TAGS = {6: "jun2026", 7: "jul2026", 8: "aug2026"}
DATA_DIR.mkdir(parents=True, exist_ok=True)

bank_open = gl_open = cents(OPENING_BALANCE)
for period_id in sorted(periods):
    p = periods[period_id]
    p["bank"].sort(key=lambda r: (r[0], r[1]))
    p["gl"].sort(key=lambda r: (r[0], r[1]))
    tag = MONTH_TAGS[p["month"]]

    # each period opens where the last one closed, per side
    bank_close = bank_open + sum(cents(r[3]) for r in p["bank"])
    gl_close = gl_open + sum(cents(r[4]) for r in p["gl"])

    with open(DATA_DIR / f"bank_statement_{tag}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Description", "Reference", "Amount"])
        for d, desc, ref, amt in p["bank"]:
            w.writerow([d.isoformat(), desc, ref, f"{amt:.2f}"])

    with open(DATA_DIR / f"gl_cash_extract_{tag}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Account", "Memo", "DocNo", "Amount"])
        for d, memo, doc, acct, amt in p["gl"]:
            w.writerow([d.isoformat(), acct, memo, doc, f"{amt:.2f}"])

    with open(DATA_DIR / f"balances_{tag}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Source", "PeriodStart", "PeriodEnd", "OpeningBalance", "ClosingBalance"])
        for src, o, c in (("BANK", bank_open, bank_close), ("GL", gl_open, gl_close)):
            w.writerow([src, p["start"].isoformat(), p["end"].isoformat(),
                        f"{o / 100:.2f}", f"{c / 100:.2f}"])

    print(f"{period_id}: bank {len(p['bank']):>3} rows, gl {len(p['gl']):>3} rows  "
          f"| bank close {bank_close / 100:>12,.2f}  gl close {gl_close / 100:>12,.2f}"
          f"  (opening gap {(bank_open - gl_open) / 100:,.2f})")
    bank_open, gl_open = bank_close, gl_close

print(f"wrote 9 files to {DATA_DIR}")