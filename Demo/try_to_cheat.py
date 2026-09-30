"""
demo/try_to_cheat.py — the accounting rules are in the database, not in the code.

Bypasses the engine entirely and attacks the ledger with raw SQL: match the
same transaction twice, book an item to nowhere, mark one resolved without
saying where, rewrite history, and edit a closed period. Every attempt is
refused by a constraint or a trigger.

    python demo/try_to_cheat.py
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import controls  # noqa: E402
import db as ledger  # noqa: E402


def attempt(conn, label, sql, params=()):
    try:
        conn.execute(sql, params)
        print(f"  NOT BLOCKED  {label}")
        conn.rollback()
    except sqlite3.IntegrityError as e:
        print(f"  blocked      {label}\n               -> {e}")


def main():
    path = ROOT / "ledger.db"
    if not path.exists():
        raise SystemExit("no ledger yet — run: python src/reconcile.py --period 2026-06")
    conn = ledger.connect(path)

    run_id = conn.execute("SELECT run_id FROM recon_run ORDER BY run_id LIMIT 1").fetchone()[0]
    bank_id, gl_id = 1, 1

    print("Attacking the ledger with raw SQL, engine bypassed:\n")

    print("One-to-one matching")
    attempt(conn, "match a bank row that is already matched",
            "INSERT INTO match (run_id, pass, method, bank_txn_id, gl_entry_id, "
            "date_delta_days, cents_delta) VALUES (?, 'EXACT', 'forged', ?, ?, 0, 0)",
            (run_id, bank_id, gl_id))

    print("\nItems must be real and honest")
    attempt(conn, "raise an item against a transaction that does not exist",
            "INSERT INTO reconciling_item (origin_period_id, origin_run_id, side, "
            "bank_txn_id, category, amount_cents) VALUES ('2026-06', ?, 'BANK', 999999, "
            "'DIT', 100)", (run_id,))
    attempt(conn, "raise an item that sits on both sides at once",
            "INSERT INTO reconciling_item (origin_period_id, origin_run_id, side, "
            "bank_txn_id, gl_entry_id, category, amount_cents) VALUES ('2026-06', ?, "
            "'BANK', ?, ?, 'DIT', 100)", (run_id, bank_id, gl_id))
    attempt(conn, "clear an item without recording where it cleared",
            "UPDATE reconciling_item SET status = 'CLEARED' WHERE item_id = "
            "(SELECT MIN(item_id) FROM reconciling_item WHERE status = 'OPEN')")

    print("\nHistory is append-only")
    attempt(conn, "rewrite an item's history",
            "UPDATE item_event SET note = 'never happened' WHERE event_id = 1")

    print("\nClosed books stay closed")
    period = conn.execute("SELECT period_id, status FROM recon_period "
                          "ORDER BY period_id LIMIT 1").fetchone()
    pid = period["period_id"]
    closed_here = period["status"] != "CLOSED"
    if closed_here:
        controls.close_period(conn, pid, "closed for the demo")
        print(f"  (closed {pid} first)")
    attempt(conn, "slip an extra transaction into a closed period",
            "INSERT INTO bank_txn (period_id, txn_date, description, amount_cents, "
            "source_file, source_row, row_hash) VALUES (?, '2026-06-15', 'SNEAKY', "
            "-100000, 'forged', 9999, 'x')", (pid,))
    attempt(conn, "restate a closed period's item",
            "UPDATE reconciling_item SET amount_cents = 1 WHERE origin_period_id = ?", (pid,))
    attempt(conn, "delete a closed period's run",
            "DELETE FROM recon_run WHERE period_id = ?", (pid,))

    print("\nBut a closed period's items can still be RESOLVED later,")
    print("because June's outstanding check has to clear in July:")
    item = conn.execute("SELECT item_id FROM reconciling_item WHERE origin_period_id = ? "
                        "AND status = 'OPEN' LIMIT 1", (pid,)).fetchone()
    conn.execute("UPDATE reconciling_item SET status = 'CLEARED', "
                 "resolved_period_id = '2026-07' WHERE item_id = ?", (item["item_id"],))
    print(f"  allowed      resolve item {item['item_id']} of closed period {pid}")
    conn.rollback()

    if closed_here:                     # leave the ledger as we found it
        controls.reopen_period(conn, pid, "demo finished")
        print(f"\n  (reopened {pid}; see: python src/controls.py --history)")
    conn.close()


if __name__ == "__main__":
    main()