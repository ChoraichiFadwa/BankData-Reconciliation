"""
export_powerbi.py — write the dashboard's tables from the ledger.

Power BI Desktop reads these CSVs (Get Data > Text/CSV, or Folder). A direct
SQLite connection needs a third-party ODBC driver on every machine that opens
the report; a folder of CSVs refreshes anywhere and versions cleanly.

Every table comes from code that already exists and is tested:
  fact_period      v_period_kpis (reporting.sql) -> rollforward + match rate
  fact_open_item   controls.aging() for EVERY period -> aging, buckets, flags
  dim_category     reconcile.CATEGORIES -> cause, action, owner, group
  dim_bucket       the four aging buckets, with a sort order

Before writing anything, the export checks that it agrees with the
rollforward: the open items it exports for a period must add up, count and
dollars, to that period's closing figure. A dashboard that disagrees with the
controls is refused, not published.

    python src/export_powerbi.py            # writes powerbi/data/*.csv
    python src/export_powerbi.py --db ledger.db --out powerbi/data
"""

import argparse
import csv
from pathlib import Path

import controls
import db
import reconcile as rec

ROOT = Path(__file__).resolve().parent.parent
REPORTING_SQL = ROOT / "sql" / "reporting.sql"

BUCKETS = [("0-30", 1), ("31-60", 2), ("61-90", 3), ("90+", 4)]

# Which group a category belongs to, and whose desk it lands on.
# Accountant: book the JE or chase the timing item. Controller: anything
# nobody can explain yet, or money that went missing.
GROUPS = {
    "BANK_CHARGE": ("Book a JE", "Accountant"),
    "INTEREST_INC": ("Book a JE", "Accountant"),
    "INTEREST_EXP": ("Book a JE", "Accountant"),
    "NSF_RETURN": ("Book a JE", "Accountant"),
    "OS_CHECK": ("Timing", "Accountant"),
    "PMT_IN_TRANSIT": ("Timing", "Accountant"),
    "DIT": ("Timing", "Accountant"),
    "DUPLICATE": ("Reverse a JE", "Accountant"),
    "UNID_CREDIT": ("Investigate", "Controller"),
    "UNID_DEBIT": ("Investigate", "Controller"),
    "PMT_NOT_DEBITED": ("Investigate", "Controller"),
    "DEP_NOT_CREDITED": ("Investigate", "Controller"),
    "RESIDUAL": ("Write-off", "Accountant"),
}


def ensure_views(conn):
    conn.executescript(REPORTING_SQL.read_text(encoding="utf-8"))


def period_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM v_period_kpis ORDER BY period_id")]


def open_item_rows(conn, periods):
    rows = []
    for p in periods:
        for r in controls.aging(conn, p["period_id"]):
            rows.append({
                "as_of_period": p["period_id"],
                "item_id": r["item_id"],
                "origin_period": r["origin_period_id"],
                "category": r["category"],
                "side": r["side"],
                "origin_date": r["origin_date"],
                "description": r["description"],
                "doc_ref": r["doc_ref"],
                "amount": r["amount_cents"] / 100,
                "abs_amount": abs(r["amount_cents"]) / 100,
                "age_days": r["age_days"],
                "bucket": r["bucket"],
                "flag": r["flag"],
                "escalated": int(bool(r["escalated"])),
                "_cents": r["amount_cents"],
            })
    return rows


def category_rows(categories_in_data):
    codes = sorted(set(rec.CATEGORIES) | set(categories_in_data))
    out = []
    for code in codes:
        cause, action = rec.CATEGORIES.get(code, (code.replace("_", " ").title(), ""))
        group, owner = GROUPS.get(code, ("Other", "Controller"))
        out.append({"category": code, "cause": cause, "action": action,
                    "group": group, "owner": owner})
    return out


def check_against_rollforward(periods, items):
    """The export must say exactly what the controls say."""
    problems = []
    for p in periods:
        mine = [i for i in items if i["as_of_period"] == p["period_id"]]
        if len(mine) != p["closing_count"]:
            problems.append(f"{p['period_id']}: exported {len(mine)} open items, "
                            f"rollforward says {p['closing_count']}")
        cents = sum(i["_cents"] for i in mine)
        if cents != p["closing_cents"]:
            problems.append(f"{p['period_id']}: exported {cents / 100:,.2f}, "
                            f"rollforward says {p['closing_cents'] / 100:,.2f}")
        if p["rollforward_status"] != "BALANCED":
            problems.append(f"{p['period_id']}: rollforward does not balance")
    return problems


def write_csv(path, rows, drop=("_cents",)):
    rows = [{k: v for k, v in r.items() if k not in drop} for r in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        if not rows:
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def export(db_path, out_dir) -> dict:
    conn = db.connect(db_path)
    ensure_views(conn)
    periods = period_rows(conn)
    if not periods:
        raise SystemExit("no reconciled periods in the ledger: run reconcile.py first")
    items = open_item_rows(conn, periods)

    problems = check_against_rollforward(periods, items)
    if problems:
        raise SystemExit("export refused, it disagrees with the controls:\n  "
                         + "\n  ".join(problems))

    for p in periods:   # dollars for Power BI, cents stay in the ledger
        for k in [k for k in p if k.endswith("_cents")]:
            p[k.replace("_cents", "")] = p.pop(k) / 100

    out = Path(out_dir)
    write_csv(out / "fact_period.csv", periods)
    write_csv(out / "fact_open_item.csv", items)
    write_csv(out / "dim_category.csv", category_rows({i["category"] for i in items}))
    write_csv(out / "dim_bucket.csv", [{"bucket": b, "bucket_order": n} for b, n in BUCKETS])
    return {"periods": len(periods), "open_item_rows": len(items), "out": str(out)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", default=str(ROOT / "ledger.db"))
    ap.add_argument("--out", default=str(ROOT / "powerbi" / "data"))
    a = ap.parse_args()
    print(export(a.db, a.out))