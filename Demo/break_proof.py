"""
demo/break_proof.py — show that the proof can actually fail.

Deletes one row from a copy of the bank statement and reconciles the damaged
file. A proof computed from the rows themselves would still tie; this one
breaks by exactly the amount of the missing row, and the completeness control
says which file is short.

    python demo/break_proof.py
"""

import csv
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import reconcile as rec  # noqa: E402

PERIOD_FILES = {
    "bank": ROOT / "data" / "bank_statement_jun2026.csv",
    "gl": ROOT / "data" / "gl_cash_extract_jun2026.csv",
    "balances": ROOT / "data" / "balances_jun2026.csv",
}
VICTIM_ROW = 40          # any matched row will do


def money(cents):
    return f"{cents / 100:,.2f}"


def main():
    *_, proof = rec.reconcile(PERIOD_FILES["bank"], PERIOD_FILES["gl"],
                              PERIOD_FILES["balances"])
    print("Intact June:")
    print(f"  bank completeness {money(proof['completeness']['Bank']['diff']):>12}")
    print(f"  unreconciled diff {money(proof['difference']):>12}   "
          f"{'TIES' if proof['difference'] == 0 else 'DOES NOT TIE'}")

    with tempfile.TemporaryDirectory() as tmp:
        damaged = Path(tmp) / "bank_missing_one_row.csv"
        with open(PERIOD_FILES["bank"], newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        removed = rows.pop(VICTIM_ROW)
        with open(damaged, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

        print(f"\nRemoved one bank row: {removed[0]}  {removed[1][:38]}  {removed[3]}")
        *_, proof = rec.reconcile(damaged, PERIOD_FILES["gl"], PERIOD_FILES["balances"])
        print("Damaged June:")
        print(f"  bank completeness {money(proof['completeness']['Bank']['diff']):>12}"
              f"   <- the bank file no longer rolls to its stated closing balance")
        print(f"  GL completeness   {money(proof['completeness']['GL']['diff']):>12}"
              f"   <- the GL file is fine, so the break is on the bank side")
        print(f"  unreconciled diff {money(proof['difference']):>12}   "
              f"{'TIES' if proof['difference'] == 0 else 'DOES NOT TIE'}")
        print("\nThe first version of this project reported 0.00 here: the proof was\n"
              "computed from the rows, so it tied by construction whatever the input.")


if __name__ == "__main__":
    main()