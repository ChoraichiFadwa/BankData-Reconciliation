"""
db.py — the ledger: create the SQLite database and load a period into it.

The engine is unchanged by this module. All it does is put the three source
files of a period into tables, so later phases can ask questions the CSVs
can't answer: what is still open from last month, how long has it been open,
and did anything get lost between one period and the next.

Loading is idempotent. Each row is keyed by (period, file, line number), so
reloading the same file changes nothing. A file whose CONTENT changed is
refused unless --replace is passed, because silently keeping the old rows
would poison every number built on them.

Usage:
    python src/db.py --init                       # create ledger.db from sql/schema.sql
    python src/db.py --load 2026-06               # load that period's three CSVs
    python src/db.py --load 2026-06 --replace     # reload after the data changed
    python src/db.py --status                     # what the ledger currently holds
"""

import argparse
import csv
import hashlib
import sqlite3
from datetime import date
from pathlib import Path

from reconcile import check_number, to_cents

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "sql" / "schema.sql"
DEFAULT_DB = ROOT / "ledger.db"
DEFAULT_DATA = ROOT / "data"


# ------------------------------------------------------------------ connection
def connect(db_path=DEFAULT_DB) -> sqlite3.Connection:
    """Open a connection with the guarantees the schema assumes."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")    # OFF by default in SQLite, per connection
    return conn


def init_db(db_path=DEFAULT_DB) -> Path:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return Path(db_path)


# ------------------------------------------------------------------ file naming
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]


def period_files(period_id: str, data_dir=DEFAULT_DATA) -> dict:
    """'2026-06' -> the three June 2026 CSVs."""
    year, month = period_id.split("-")
    tag = f"{MONTHS[int(month) - 1]}{year}"
    return {"bank": Path(data_dir) / f"bank_statement_{tag}.csv",
            "gl": Path(data_dir) / f"gl_cash_extract_{tag}.csv",
            "balances": Path(data_dir) / f"balances_{tag}.csv"}


def read_csv_rows(path: Path) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def row_hash(*parts) -> str:
    """Content hash INCLUDING the line number.

    Hashing content alone would treat a genuine duplicate posting — same date,
    memo, document and amount as another line — as a re-load and drop it. That
    is the exact break the engine exists to catch, so the line number is part
    of the identity of a row."""
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ loading
def load_period(conn, period_id: str, data_dir=DEFAULT_DATA, replace=False) -> dict:
    """Load one period's three CSVs. Returns counts. Idempotent."""
    files = period_files(period_id, data_dir)
    missing = [str(p) for p in files.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing input files: {missing}")

    balances = {r["Source"].strip().upper(): r for r in read_csv_rows(files["balances"])}
    if {"BANK", "GL"} - balances.keys():
        raise ValueError(f"{files['balances'].name} needs a BANK row and a GL row")
    bank_rows = read_csv_rows(files["bank"])
    gl_rows = read_csv_rows(files["gl"])

    period = conn.execute("SELECT * FROM recon_period WHERE period_id = ?",
                          (period_id,)).fetchone()
    if period and period["status"] == "CLOSED":
        raise PermissionError(f"period {period_id} is closed — reopen it before reloading")

    def hashes(table, id_col):
        rows = conn.execute(f"SELECT row_hash FROM {table} WHERE period_id = ?",
                            (period_id,)).fetchall()
        return sorted(r["row_hash"] for r in rows)

    def bank_hash(i, r):
        return row_hash(period_id, files["bank"].name, i,
                        r["Date"], r["Description"], r["Reference"], r["Amount"])

    def gl_hash(i, r):
        return row_hash(period_id, files["gl"].name, i,
                        r["Date"], r["Account"], r["Memo"], r["DocNo"], r["Amount"])

    incoming = sorted([bank_hash(i, r) for i, r in enumerate(bank_rows, 2)]
                      + [gl_hash(i, r) for i, r in enumerate(gl_rows, 2)])
    existing = sorted(hashes("bank_txn", "bank_txn_id") + hashes("gl_entry", "gl_entry_id"))

    if existing and existing == incoming and not replace:
        return {"period_id": period_id, "bank": 0, "gl": 0, "action": "unchanged"}
    if existing and existing != incoming and not replace:
        raise ValueError(
            f"period {period_id} is already loaded with different data. "
            f"Re-run with --replace to discard the stored rows and load these.")

    with conn:                                   # one transaction: all or nothing
        if existing:
            conn.execute("DELETE FROM bank_txn WHERE period_id = ?", (period_id,))
            conn.execute("DELETE FROM gl_entry WHERE period_id = ?", (period_id,))
        conn.execute("""
            INSERT INTO recon_period (period_id, period_start, period_end,
                                      bank_open_cents, bank_close_cents,
                                      gl_open_cents, gl_close_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (period_id) DO UPDATE SET
                period_start = excluded.period_start, period_end = excluded.period_end,
                bank_open_cents = excluded.bank_open_cents,
                bank_close_cents = excluded.bank_close_cents,
                gl_open_cents = excluded.gl_open_cents,
                gl_close_cents = excluded.gl_close_cents
        """, (period_id,
              balances["BANK"]["PeriodStart"], balances["BANK"]["PeriodEnd"],
              to_cents(balances["BANK"]["OpeningBalance"]),
              to_cents(balances["BANK"]["ClosingBalance"]),
              to_cents(balances["GL"]["OpeningBalance"]),
              to_cents(balances["GL"]["ClosingBalance"])))

        conn.executemany("""
            INSERT INTO bank_txn (period_id, txn_date, description, reference,
                                  amount_cents, check_no, source_file, source_row, row_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [(period_id, r["Date"], r["Description"], r["Reference"],
               to_cents(r["Amount"]), check_number(r["Reference"]),
               files["bank"].name, i, bank_hash(i, r))
              for i, r in enumerate(bank_rows, 2)])

        conn.executemany("""
            INSERT INTO gl_entry (period_id, txn_date, account, memo, doc_no,
                                  amount_cents, check_no, source_file, source_row, row_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [(period_id, r["Date"], r["Account"], r["Memo"], r["DocNo"],
               to_cents(r["Amount"]), check_number(r["DocNo"]),
               files["gl"].name, i, gl_hash(i, r))
              for i, r in enumerate(gl_rows, 2)])

    return {"period_id": period_id, "bank": len(bank_rows), "gl": len(gl_rows),
            "action": "replaced" if existing else "loaded"}


# ------------------------------------------------------------------ reading back
def period_frames(conn, period_id: str):
    """The stored period in the shape the engine already speaks: (bank, gl, balances).

    Phase 2 swaps reconcile.load() for this; nothing else in the engine changes."""
    import pandas as pd

    p = conn.execute("SELECT * FROM recon_period WHERE period_id = ?", (period_id,)).fetchone()
    if p is None:
        raise LookupError(f"period {period_id} is not in the ledger")

    def frame(sql, rename):
        df = pd.read_sql_query(sql, conn, params=(period_id,), parse_dates=["Date"])
        return df.rename(columns=rename)

    bank = frame("""SELECT txn_date AS Date, description AS Description, reference AS Reference,
                           amount_cents AS cents, check_no AS chk, source_row AS src_row
                    FROM bank_txn WHERE period_id = ? ORDER BY source_row""", {})
    gl = frame("""SELECT txn_date AS Date, account AS Account, memo AS Memo, doc_no AS DocNo,
                         amount_cents AS cents, check_no AS chk, source_row AS src_row
                  FROM gl_entry WHERE period_id = ? ORDER BY source_row""", {})
    for df in (bank, gl):
        df["Amount"] = df["cents"] / 100
        df["chk"] = df["chk"].fillna("")

    balances = {
        "BANK": {"start": date.fromisoformat(p["period_start"]),
                 "end": date.fromisoformat(p["period_end"]),
                 "open": p["bank_open_cents"], "close": p["bank_close_cents"]},
        "GL": {"start": date.fromisoformat(p["period_start"]),
               "end": date.fromisoformat(p["period_end"]),
               "open": p["gl_open_cents"], "close": p["gl_close_cents"]},
        "period_end": __import__("pandas").Timestamp(p["period_end"]),
    }
    return bank, gl, balances


def status(conn) -> list:
    return conn.execute("""
        SELECT p.period_id, p.status, p.period_end,
               (SELECT COUNT(*) FROM bank_txn b WHERE b.period_id = p.period_id) AS bank_rows,
               (SELECT COUNT(*) FROM gl_entry g WHERE g.period_id = p.period_id) AS gl_rows,
               (SELECT COUNT(*) FROM recon_run r WHERE r.period_id = p.period_id) AS runs,
               (SELECT COUNT(*) FROM reconciling_item i
                 WHERE i.origin_period_id = p.period_id AND i.status = 'OPEN') AS open_items
        FROM recon_period p ORDER BY p.period_id
    """).fetchall()


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Reconciliation ledger — create and load")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA))
    ap.add_argument("--init", action="store_true", help="create the database")
    ap.add_argument("--load", metavar="PERIOD", help="load a period, e.g. 2026-06")
    ap.add_argument("--replace", action="store_true", help="discard stored rows and reload")
    ap.add_argument("--status", action="store_true", help="show what the ledger holds")
    args = ap.parse_args()

    if args.init or not Path(args.db).exists():
        print(f"Initialised {init_db(args.db)}")

    conn = connect(args.db)
    if args.load:
        r = load_period(conn, args.load, args.data_dir, replace=args.replace)
        print(f"Period {r['period_id']}: {r['action']} "
              f"(bank {r['bank']} rows, GL {r['gl']} rows)")
    if args.status or args.load:
        print(f"{'period':>8} {'status':>7} {'bank':>6} {'gl':>6} {'runs':>5} {'open items':>11}")
        for row in status(conn):
            print(f"{row['period_id']:>8} {row['status']:>7} {row['bank_rows']:>6} "
                  f"{row['gl_rows']:>6} {row['runs']:>5} {row['open_items']:>11}")
    conn.close()


if __name__ == "__main__":
    main()