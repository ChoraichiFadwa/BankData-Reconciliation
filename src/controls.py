"""
controls.py — the month-over-month controls the ledger exists to support.

Three of them, and each answers a question the single-month engine could not:

  rollforward  did anything get LOST between periods?
               open at start + raised − cleared − written off = open at end,
               checked against the items actually open, and against the next
               period's opening figure.

  aging        how long has each open item been open, and what does that mean?
               A deposit in transit at 3 days is routine; the same item at 80
               days means the money never arrived.

  close        once the books are closed they stay closed — enforced by
               triggers in the schema, not by this module.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGING_SQL = ROOT / "sql" / "aging.sql"

# Thresholds, in days from the item's own transaction date.
FOLLOW_UP_DAYS = 60     # an outstanding check older than this needs chasing
ESCALATE_DAYS = 30      # unexplained money, or money the bank never saw
STALE_DAYS = 180        # a check the bank will no longer honour

FLAGS_THAT_ESCALATE = {"ESCALATE", "STALE"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ rollforward
def rollforward(conn, period_id=None) -> list:
    """The rollforward for every period, or one of them."""
    sql = "SELECT * FROM v_rollforward_check"
    params = ()
    if period_id:
        sql += " WHERE period_id = ?"
        params = (period_id,)
    return conn.execute(sql + " ORDER BY period_id", params).fetchall()


def rollforward_breaks(conn) -> list:
    """Every way the rollforward can fail, as plain sentences.

    Three independent checks: each period's own arithmetic, continuity
    between periods (one period's closing items are the next one's opening
    items), and agreement with what each run recorded at the time.

    The third matters because the first two are computed from the item table
    itself: an item DELETED from it vanishes from both sides of the equation
    and still balances. The run's own count of what it raised is the outside
    witness — and unlike the closing figure, it cannot legitimately change
    after the run (a later write-off moves an item out, which is correct)."""
    rows = rollforward(conn)
    breaks = []
    recorded = {r["period_id"]: r for r in conn.execute(
        "SELECT period_id, n_raised, n_exceptions FROM recon_run")}
    for r in rows:
        run = recorded.get(r["period_id"])
        if run and run["n_raised"] != r["raised_count"]:
            breaks.append(
                f"{r['period_id']}: the run recorded {run['n_raised']} items raised, "
                f"but {r['raised_count']} are in the ledger")
        if r["status"] != "BALANCED":
            breaks.append(
                f"{r['period_id']}: {r['opening_count']} open + {r['raised_count']} raised "
                f"− {r['cleared_count']} cleared − {r['written_off_count']} written off "
                f"= {r['derived_count']}, but {r['closing_count']} items are actually open")
    for prev, nxt in zip(rows, rows[1:]):
        if prev["closing_count"] != nxt["opening_count"] or \
                prev["closing_cents"] != nxt["opening_cents"]:
            breaks.append(
                f"{prev['period_id']} closed with {prev['closing_count']} open items "
                f"({prev['closing_cents'] / 100:,.2f}) but {nxt['period_id']} opened with "
                f"{nxt['opening_count']} ({nxt['opening_cents'] / 100:,.2f})")
    return breaks


# ------------------------------------------------------------------ aging
def aging(conn, period_id: str, follow_up_days=FOLLOW_UP_DAYS,
          escalate_days=ESCALATE_DAYS, stale_days=STALE_DAYS) -> list:
    """Every item open at the end of `period_id`, aged and flagged."""
    return conn.execute(AGING_SQL.read_text(encoding="utf-8"),
                        {"period_id": period_id, "follow_up_days": follow_up_days,
                         "escalate_days": escalate_days, "stale_days": stale_days}).fetchall()


def aging_summary(conn, period_id: str) -> list:
    """Bucket totals — the shape a controller reads first."""
    rows = aging(conn, period_id)
    order = ["0-30", "31-60", "61-90", "90+"]
    buckets = {b: {"bucket": b, "count": 0, "cents": 0, "flagged": 0} for b in order}
    for r in rows:
        b = buckets[r["bucket"]]
        b["count"] += 1
        b["cents"] += r["amount_cents"]
        b["flagged"] += r["flag"] != "NONE"
    return [buckets[b] for b in order if buckets[b]["count"]]


def apply_escalations(conn, period_id: str, **thresholds) -> list:
    """Mark items that have crossed a threshold, once.

    Escalation is recorded on the item and in its history rather than
    recomputed silently each month, so "this was escalated in July" survives
    even if the thresholds are changed later."""
    newly = [r for r in aging(conn, period_id, **thresholds)
             if r["flag"] in FLAGS_THAT_ESCALATE and not r["escalated"]]
    with conn:
        for r in newly:
            conn.execute("UPDATE reconciling_item SET escalated = 1 WHERE item_id = ?",
                         (r["item_id"],))
            conn.execute("""
                INSERT INTO item_event (item_id, event_at, event_type, period_id, note)
                VALUES (?, ?, 'ESCALATED', ?, ?)
            """, (r["item_id"], _now(), period_id,
                  f"{r['category']} open {r['age_days']} days at {period_id} close "
                  f"({r['flag']})"))
    return newly


# ------------------------------------------------------------------ closing the books
def close_period(conn, period_id: str, note=None, force=False) -> dict:
    """Close a period. Refuses if the close is not actually finished.

    The checks are the point: a period that does not tie, whose inputs are
    incomplete, or that was never reconciled at all, is not a period anyone
    should be able to close by accident."""
    period = conn.execute("SELECT * FROM recon_period WHERE period_id = ?",
                          (period_id,)).fetchone()
    if period is None:
        raise LookupError(f"period {period_id} is not in the ledger")
    if period["status"] == "CLOSED":
        raise PermissionError(f"period {period_id} is already closed")

    run = conn.execute("SELECT * FROM recon_run WHERE period_id = ? ORDER BY run_id DESC LIMIT 1",
                       (period_id,)).fetchone()
    problems = []
    if run is None:
        problems.append("it has never been reconciled")
    else:
        if run["proof_diff_cents"]:
            problems.append(f"the proof is out by {run['proof_diff_cents'] / 100:,.2f}")
        if run["bank_control_cents"]:
            problems.append(f"the bank file is incomplete by "
                            f"{run['bank_control_cents'] / 100:,.2f}")
        if run["gl_control_cents"]:
            problems.append(f"the GL file is incomplete by "
                            f"{run['gl_control_cents'] / 100:,.2f}")
    breaks = rollforward_breaks(conn)
    problems += breaks
    if problems and not force:
        raise ValueError(f"cannot close {period_id}: " + "; ".join(problems))

    with conn:
        conn.execute("UPDATE recon_period SET status = 'CLOSED', closed_at = ? "
                     "WHERE period_id = ?", (_now(), period_id))
        conn.execute("""INSERT INTO period_event (period_id, event_at, event_type, note)
                        VALUES (?, ?, 'CLOSED', ?)""",
                     (period_id, _now(),
                      note or ("closed with overrides: " + "; ".join(problems)
                               if problems else "closed after a clean reconciliation")))
    return {"period_id": period_id, "open_items": open_item_count(conn, period_id),
            "overrides": problems}


def reopen_period(conn, period_id: str, reason: str) -> None:
    """Reopening closed books is legitimate and must never be silent."""
    if not reason or not reason.strip():
        raise ValueError("reopening a closed period requires a reason")
    period = conn.execute("SELECT status FROM recon_period WHERE period_id = ?",
                          (period_id,)).fetchone()
    if period is None:
        raise LookupError(f"period {period_id} is not in the ledger")
    if period["status"] != "CLOSED":
        raise PermissionError(f"period {period_id} is not closed")
    with conn:
        conn.execute("UPDATE recon_period SET status = 'OPEN', closed_at = NULL "
                     "WHERE period_id = ?", (period_id,))
        conn.execute("""INSERT INTO period_event (period_id, event_at, event_type, note)
                        VALUES (?, ?, 'REOPENED', ?)""", (period_id, _now(), reason))


def open_item_count(conn, period_id: str) -> int:
    row = conn.execute("SELECT closing_count FROM v_rollforward WHERE period_id = ?",
                       (period_id,)).fetchone()
    return row["closing_count"] if row else 0


def period_history(conn, period_id=None) -> list:
    sql = "SELECT * FROM period_event"
    params = ()
    if period_id:
        sql += " WHERE period_id = ?"
        params = (period_id,)
    return conn.execute(sql + " ORDER BY event_id", params).fetchall()


# ------------------------------------------------------------------ printing
def print_rollforward(conn):
    print(f"{'period':>8} {'opening':>9} {'raised':>7} {'cleared':>8} {'w/off':>6} "
          f"{'closing':>8}  {'exposure':>14}  status")
    for r in rollforward(conn):
        print(f"{r['period_id']:>8} {r['opening_count']:>9} {r['raised_count']:>7} "
              f"{r['cleared_count']:>8} {r['written_off_count']:>6} {r['closing_count']:>8}  "
              f"{r['closing_cents'] / 100:>14,.2f}  {r['status']}")
    for line in rollforward_breaks(conn):
        print(f"  BREAK: {line}")


def print_aging(conn, period_id):
    print(f"Open items at {period_id} close")
    for b in aging_summary(conn, period_id):
        print(f"  {b['bucket']:>6} days: {b['count']:>2} items  "
              f"{b['cents'] / 100:>14,.2f}  ({b['flagged']} flagged)")
    for r in aging(conn, period_id):
        if r["flag"] != "NONE":
            print(f"  {r['flag']:<9} {r['category']:<17} {r['amount_cents'] / 100:>12,.2f}  "
                  f"{r['age_days']:>3} days  from {r['origin_period_id']}  "
                  f"{str(r['description'])[:44]}")


# ------------------------------------------------------------------ CLI
def main():
    import db

    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description="Month-over-month controls: rollforward, aging, closing the books",
        epilog="examples:  controls.py --rollforward   |   controls.py --aging 2026-08   |   "
               "controls.py --close 2026-06 --note 'signed off by JT'")
    ap.add_argument("--db", default=str(root / "ledger.db"))
    ap.add_argument("--rollforward", action="store_true",
                    help="show the rollforward for every period and any breaks")
    ap.add_argument("--aging", metavar="PERIOD", help="show open items aged at this period's close")
    ap.add_argument("--escalate", metavar="PERIOD",
                    help="flag items that have crossed a threshold at this period's close")
    ap.add_argument("--close", metavar="PERIOD", help="close a period")
    ap.add_argument("--note", help="note recorded with --close")
    ap.add_argument("--force", action="store_true",
                    help="close despite unresolved problems (the overrides are recorded)")
    ap.add_argument("--reopen", metavar="PERIOD", help="reopen a closed period")
    ap.add_argument("--reason", help="why the period is being reopened (required by --reopen)")
    ap.add_argument("--history", action="store_true", help="show every close and reopen")
    args = ap.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"no ledger at {args.db} — run reconcile.py --period first")
    conn = db.connect(args.db)
    try:
        did = False
        if args.escalate:
            newly = apply_escalations(conn, args.escalate)
            print(f"{len(newly)} item(s) newly escalated at {args.escalate} close")
            did = True
        if args.aging:
            print_aging(conn, args.aging)
            did = True
        if args.close:
            result = close_period(conn, args.close, args.note, force=args.force)
            print(f"Closed {args.close}: {result['open_items']} items still open"
                  + (f" (overrides: {'; '.join(result['overrides'])})" if result["overrides"] else ""))
            did = True
        if args.reopen:
            if not args.reason:
                raise SystemExit("--reopen needs --reason: reopening closed books is never silent")
            reopen_period(conn, args.reopen, args.reason)
            print(f"Reopened {args.reopen}: {args.reason}")
            did = True
        if args.history:
            for e in period_history(conn):
                print(f"  {e['event_at'][:19]}  {e['period_id']}  {e['event_type']:9} {e['note']}")
            did = True
        if args.rollforward or not did:
            print_rollforward(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()