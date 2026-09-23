"""
carryforward.py — Pass 0.

The other three passes reconcile a month against itself. Pass 0 reconciles it
against the past: before any of them run, it asks the ledger what is still
open from earlier periods and clears whatever this month's transactions
resolve.

Without it, every carried item produces TWO false exceptions in the new month
— the bank row that cleared it has no partner in the new GL, and the item is
still sitting open — and the proof misses by the opening gap between the bank
and the GL.

This module decides; it does not write. store.save_run() persists the
resolutions inside the run's transaction, so a failed run leaves the ledger
exactly as it was.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS0_SQL = ROOT / "sql" / "pass0_carryforward.sql"

# Strictest rule first: a matching check number is proof, a bare amount is
# circumstantial, so check numbers get first claim on a transaction.
RULE_ORDER = {"check no.": 0, "reversal": 1, "residual write-off": 2,
              "booked in GL": 3, "amount": 4}

CATEGORY_TEXT = {
    "OS_CHECK": "cleared the bank",
    "PMT_IN_TRANSIT": "settled at the bank",
    "PMT_NOT_DEBITED": "finally debited by the bank",
    "DIT": "credited by the bank",
    "DEP_NOT_CREDITED": "finally credited by the bank",
    "BANK_CHARGE": "booked in the GL",
    "INTEREST_INC": "booked in the GL",
    "INTEREST_EXP": "booked in the GL",
    "NSF_RETURN": "booked in the GL",
    "UNID_CREDIT": "identified and booked",
    "UNID_DEBIT": "identified and booked",
    "DUPLICATE": "reversed in the GL",
    "RESIDUAL": "written off in the GL",
}


def open_items(conn, period_id: str) -> list:
    """Items raised before this period and open as of its start — the opening
    balance of the rollforward.

    "Open as of its start" includes items a previous run of THIS period
    cleared: re-running takes those clearings back, so from the new run's point
    of view they are open again. Without that, a re-run would quietly orphan
    every item its predecessor had cleared."""
    return conn.execute("""
        SELECT i.item_id, i.side, i.category, i.amount_cents, i.origin_period_id,
               COALESCE(b.txn_date, g.txn_date)       AS origin_date,
               COALESCE(b.description, g.memo)        AS description,
               COALESCE(b.reference, g.doc_no, '')    AS doc_ref,
               COALESCE(b.source_row, g.source_row)   AS source_row
        FROM reconciling_item i
        LEFT JOIN bank_txn b ON b.bank_txn_id = i.bank_txn_id
        LEFT JOIN gl_entry g ON g.gl_entry_id = i.gl_entry_id
        WHERE (i.status = 'OPEN' OR i.resolved_period_id = ?)
          AND i.origin_period_id < ?
        ORDER BY i.origin_period_id, ABS(i.amount_cents) DESC, i.item_id
    """, (period_id, period_id)).fetchall()


def candidates(conn, period_id: str) -> list:
    """Every (item, transaction) pair this period could clear. Read-only."""
    return conn.execute(PASS0_SQL.read_text(encoding="utf-8"),
                        {"period_id": period_id}).fetchall()


def resolve(conn, period_id: str) -> dict:
    """Assign candidates one to one and report what clears and what does not.

    Nothing is written here. The result feeds reconcile() (which must not
    match a consumed row again) and save_run() (which persists it)."""
    rows = candidates(conn, period_id)
    rows.sort(key=lambda r: (RULE_ORDER.get(r["rule"], 9), r["clearing_date"],
                             abs(r["amount_cents"]) * -1, r["item_id"]))

    resolutions, taken_items, used_bank, used_gl = [], set(), set(), set()
    for r in rows:
        on_bank = r["clearing_side"] == "BANK"
        used = used_bank if on_bank else used_gl
        if r["item_id"] in taken_items or r["clearing_id"] in used:
            continue
        taken_items.add(r["item_id"])
        used.add(r["clearing_id"])
        resolutions.append({
            "item_id": r["item_id"],
            "category": r["category"],
            "amount_cents": r["amount_cents"],
            "origin_period": r["origin_period_id"],
            "origin_date": r["origin_date"],
            "clearing_side": r["clearing_side"],
            "clearing_id": r["clearing_id"],
            "clearing_date": r["clearing_date"],
            "clearing_text": r["clearing_text"],
            "rule": r["rule"],
            "note": f"{r['category']} from {r['origin_period_id']} "
                    f"{CATEGORY_TEXT.get(r['category'], 'resolved')} on {r['clearing_date']} "
                    f"(matched on {r['rule']})",
        })

    still_open = [dict(i) for i in open_items(conn, period_id)
                  if i["item_id"] not in taken_items]
    return {"period_id": period_id,
            "resolutions": resolutions,
            "still_open": still_open,
            "consumed_bank": used_bank,
            "consumed_gl": used_gl}