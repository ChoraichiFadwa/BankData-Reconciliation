"""
Tests for phase 5: the month-over-month controls.

  rollforward  nothing was lost between periods
  aging        how long items have been open, and what that means
  locking      closed books stay closed, enforced by the database

These are the controls that make the ledger worth having: without them it
stores history but never checks it.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import carryforward as cf  # noqa: E402
import controls  # noqa: E402
import db as ledger  # noqa: E402
import reconcile as rec  # noqa: E402
import store  # noqa: E402

DATA = ROOT / "data"
PERIODS = ["2026-06", "2026-07", "2026-08"]
STALE_DEPOSIT_CENTS = 940_658
VERMEER_CENTS = -1_589_023


def close_months(path, periods=PERIODS, escalate=True):
    conn = ledger.connect(ledger.init_db(path))
    for period_id in periods:
        ledger.load_period(conn, period_id, DATA)
        carried = cf.resolve(conn, period_id)
        bank, gl, _, matches, exceptions, proof = rec.reconcile(
            conn=conn, period_id=period_id, carried=carried)
        store.save_run(conn, period_id, bank, gl, matches, exceptions, proof, carried=carried)
        if escalate:
            controls.apply_escalations(conn, period_id)
    return conn


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    c = close_months(tmp_path_factory.mktemp("controls") / "ledger.db")
    yield c
    c.close()


@pytest.fixture
def fresh(tmp_path):
    """A private ledger for tests that modify it."""
    c = close_months(tmp_path / "ledger.db")
    yield c
    c.close()


def rf(conn, period_id):
    return controls.rollforward(conn, period_id)[0]


# ------------------------------------------------------------------ rollforward
def test_rollforward_balances_every_period(conn):
    for row in controls.rollforward(conn):
        assert row["status"] == "BALANCED", row["period_id"]
        assert (row["opening_count"] + row["raised_count"]
                - row["cleared_count"] - row["written_off_count"]) == row["closing_count"]
        assert (row["opening_cents"] + row["raised_cents"]
                - row["cleared_cents"] - row["written_off_cents"]) == row["closing_cents"]


def test_rollforward_numbers(conn):
    assert [(r["period_id"], r["opening_count"], r["raised_count"],
             r["cleared_count"], r["closing_count"]) for r in controls.rollforward(conn)] == [
        ("2026-06", 0, 19, 0, 19),
        ("2026-07", 19, 6, 15, 10),
        ("2026-08", 10, 4, 5, 9)]


def test_one_period_closes_where_the_next_opens(conn):
    rows = controls.rollforward(conn)
    for prev, nxt in zip(rows, rows[1:]):
        assert prev["closing_count"] == nxt["opening_count"]
        assert prev["closing_cents"] == nxt["opening_cents"]
    assert controls.rollforward_breaks(conn) == []


def test_rollforward_agrees_with_the_rec(conn):
    """A period's closing figure must be exactly the items on that period's
    rec, count and dollars — the two views must never drift apart."""
    for period_id in PERIODS:
        aged = controls.aging(conn, period_id)
        row = rf(conn, period_id)
        assert len(aged) == row["closing_count"]
        assert sum(r["amount_cents"] for r in aged) == row["closing_cents"]


def test_rollforward_matches_what_each_run_recorded(conn):
    """The run is the outside witness: what it counted at the time must still
    be what the ledger holds."""
    for row in controls.rollforward(conn):
        run = conn.execute("SELECT n_raised, n_exceptions FROM recon_run WHERE period_id = ?",
                           (row["period_id"],)).fetchone()
        assert row["raised_count"] == run["n_raised"]


def test_a_lost_item_breaks_the_rollforward(fresh):
    """The control's whole purpose: an item that vanishes must be caught."""
    fresh.execute("UPDATE recon_period SET status = 'OPEN' WHERE period_id = '2026-06'")
    item = fresh.execute("SELECT item_id FROM reconciling_item WHERE origin_period_id = '2026-06' "
                         "AND status = 'OPEN' LIMIT 1").fetchone()["item_id"]
    fresh.execute("DELETE FROM reconciling_item WHERE item_id = ?", (item,))

    breaks = controls.rollforward_breaks(fresh)
    assert breaks
    # the ledger alone would still balance — it is the run's own record of what
    # it raised that catches the deletion
    assert any("the run recorded" in b for b in breaks)


def test_a_silently_cleared_item_breaks_the_rollforward(fresh):
    """Marking an item resolved without it being raised or carried properly
    must not quietly reduce the closing figure."""
    fresh.execute("""UPDATE reconciling_item SET status = 'CLEARED',
                            resolved_period_id = '2026-06'
                      WHERE origin_period_id = '2026-07' AND status = 'OPEN'""")
    assert controls.rollforward_breaks(fresh)


def test_written_off_items_leave_the_rollforward(fresh):
    """A write-off is the third way out of the ledger, and the rollforward has
    to account for it exactly like a clearing."""
    before = rf(fresh, "2026-08")
    item = [r for r in controls.aging(fresh, "2026-08")
            if r["amount_cents"] == STALE_DEPOSIT_CENTS][0]
    store.write_off(fresh, item["item_id"], "2026-08", "deposit never made — written off")

    after = rf(fresh, "2026-08")
    assert after["written_off_count"] == 1
    assert after["written_off_cents"] == STALE_DEPOSIT_CENTS
    assert after["closing_count"] == before["closing_count"] - 1
    assert after["closing_cents"] == before["closing_cents"] - STALE_DEPOSIT_CENTS
    assert after["status"] == "BALANCED"
    assert controls.rollforward_breaks(fresh) == []


def test_a_write_off_needs_a_reason_in_the_history(fresh):
    item = [r for r in controls.aging(fresh, "2026-08")
            if r["amount_cents"] == STALE_DEPOSIT_CENTS][0]
    store.write_off(fresh, item["item_id"], "2026-08", "deposit never made")
    event = fresh.execute("""SELECT * FROM item_event WHERE item_id = ?
                             AND event_type = 'WRITTEN_OFF'""", (item["item_id"],)).fetchone()
    assert event["note"] == "deposit never made"
    assert event["period_id"] == "2026-08"


# ------------------------------------------------------------------ aging
def test_items_age_by_about_a_month_each_period(conn):
    ages = {}
    for period_id in ("2026-07", "2026-08"):
        row = [r for r in controls.aging(conn, period_id)
               if r["amount_cents"] == VERMEER_CENTS][0]
        ages[period_id] = row["age_days"]
    assert ages == {"2026-07": 32, "2026-08": 63}


def test_buckets_and_flags_at_august_close(conn):
    rows = {r["amount_cents"]: r for r in controls.aging(conn, "2026-08")}
    assert rows[VERMEER_CENTS]["bucket"] == "61-90"
    assert rows[VERMEER_CENTS]["flag"] == "FOLLOW_UP"      # 63 days: chase, not stale
    assert rows[STALE_DEPOSIT_CENTS]["flag"] == "ESCALATE"  # 80 days, never credited
    assert rows[215_000]["flag"] == "ESCALATE"              # unidentified credit, 66 days


def test_unexplained_money_escalates_before_a_check_does(conn):
    """A check outstanding for 60 days is ordinary; money nobody can explain
    for 30 days is not."""
    rows = {r["amount_cents"]: r for r in controls.aging(conn, "2026-07")}
    assert rows[-126_000]["age_days"] == 36 and rows[-126_000]["flag"] == "ESCALATE"
    assert rows[VERMEER_CENTS]["age_days"] == 32 and rows[VERMEER_CENTS]["flag"] == "NONE"


def test_fresh_items_are_not_flagged(conn):
    for r in controls.aging(conn, "2026-08"):
        if r["origin_period_id"] == "2026-08":
            assert r["flag"] == "NONE"


def test_residuals_are_never_flagged(conn):
    for period_id in PERIODS:
        for r in controls.aging(conn, period_id):
            if r["category"] == "RESIDUAL":
                assert r["flag"] == "NONE"


def test_aging_summary_covers_every_open_item(conn):
    rows = controls.aging(conn, "2026-08")
    summary = controls.aging_summary(conn, "2026-08")
    assert sum(b["count"] for b in summary) == len(rows)
    assert sum(b["cents"] for b in summary) == sum(r["amount_cents"] for r in rows)


def test_escalation_is_recorded_once(fresh):
    """Escalation is a decision with a date, not a recomputation."""
    escalated = fresh.execute(
        "SELECT COUNT(*) FROM reconciling_item WHERE escalated = 1").fetchone()[0]
    assert escalated == 4
    events = fresh.execute(
        "SELECT COUNT(*) FROM item_event WHERE event_type = 'ESCALATED'").fetchone()[0]
    assert events == 4

    again = controls.apply_escalations(fresh, "2026-08")   # re-running changes nothing
    assert again == []
    assert fresh.execute("SELECT COUNT(*) FROM item_event WHERE event_type = 'ESCALATED'"
                         ).fetchone()[0] == 4


def test_thresholds_are_tunable(conn):
    strict = controls.aging(conn, "2026-08", follow_up_days=1, escalate_days=1)
    assert sum(1 for r in strict if r["flag"] != "NONE") > \
           sum(1 for r in controls.aging(conn, "2026-08") if r["flag"] != "NONE")


# ------------------------------------------------------------------ closing the books
def test_closing_requires_a_clean_reconciliation(fresh):
    fresh.execute("UPDATE recon_run SET proof_diff_cents = -5000 WHERE period_id = '2026-08'")
    with pytest.raises(ValueError, match="proof is out by"):
        controls.close_period(fresh, "2026-08")
    assert fresh.execute("SELECT status FROM recon_period WHERE period_id = '2026-08'"
                         ).fetchone()[0] == "OPEN"

    result = controls.close_period(fresh, "2026-08", force=True)
    assert result["overrides"]                              # the override is recorded
    note = controls.period_history(fresh, "2026-08")[-1]["note"]
    assert "overrides" in note and "-50.00" in note


def test_closing_an_unreconciled_period_is_refused(tmp_path):
    conn = ledger.connect(ledger.init_db(tmp_path / "ledger.db"))
    ledger.load_period(conn, "2026-06", DATA)
    with pytest.raises(ValueError, match="never been reconciled"):
        controls.close_period(conn, "2026-06")
    conn.close()


def test_closed_period_rejects_every_kind_of_write(fresh):
    controls.close_period(fresh, "2026-06")
    attempts = [
        ("INSERT INTO bank_txn (period_id, txn_date, description, amount_cents, "
         "source_file, source_row, row_hash) "
         "VALUES ('2026-06', '2026-06-15', 'SNEAKY', -100, 'x', 9999, 'h')"),
        "DELETE FROM bank_txn WHERE period_id = '2026-06'",
        "DELETE FROM gl_entry WHERE period_id = '2026-06'",
        "UPDATE reconciling_item SET amount_cents = 1 WHERE origin_period_id = '2026-06'",
        "UPDATE reconciling_item SET category = 'DIT' WHERE origin_period_id = '2026-06'",
        "DELETE FROM reconciling_item WHERE origin_period_id = '2026-06'",
        "DELETE FROM recon_run WHERE period_id = '2026-06'",
    ]
    for sql in attempts:
        with pytest.raises(sqlite3.IntegrityError, match="closed"):
            fresh.execute(sql)


def test_closed_period_items_can_still_be_resolved_later(fresh):
    """June closes, but its outstanding check still clears in July. Locking the
    period must not freeze the items it raised."""
    controls.close_period(fresh, "2026-06")
    item = fresh.execute("""SELECT item_id FROM reconciling_item
                            WHERE origin_period_id = '2026-06' AND status = 'OPEN'
                            LIMIT 1""").fetchone()["item_id"]
    fresh.execute("""UPDATE reconciling_item
                        SET status = 'CLEARED', resolved_period_id = '2026-08'
                      WHERE item_id = ?""", (item,))
    fresh.execute("""INSERT INTO item_event (item_id, event_at, event_type, period_id, note)
                     VALUES (?, '2026-09-01T00:00:00', 'CLEARED', '2026-08', 'late clear')""",
                  (item,))
    assert fresh.execute("SELECT status FROM reconciling_item WHERE item_id = ?",
                         (item,)).fetchone()[0] == "CLEARED"


def test_history_cannot_be_edited(fresh):
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        fresh.execute("UPDATE item_event SET note = 'rewritten' WHERE event_id = 1")


def test_closed_period_cannot_be_reconciled_again(fresh):
    controls.close_period(fresh, "2026-08")
    with pytest.raises(PermissionError):
        ledger.load_period(fresh, "2026-08", DATA, replace=True)
    carried = cf.resolve(fresh, "2026-08")
    bank, gl, _, matches, exceptions, proof = rec.reconcile(
        conn=fresh, period_id="2026-08", carried=carried)
    with pytest.raises(PermissionError):
        store.save_run(fresh, "2026-08", bank, gl, matches, exceptions, proof, carried=carried)


def test_reopening_needs_a_reason_and_leaves_a_trace(fresh):
    controls.close_period(fresh, "2026-07", "July signed off")
    with pytest.raises(ValueError, match="reason"):
        controls.reopen_period(fresh, "2026-07", "   ")

    controls.reopen_period(fresh, "2026-07", "restating a misposted vendor invoice")
    assert fresh.execute("SELECT status FROM recon_period WHERE period_id = '2026-07'"
                         ).fetchone()[0] == "OPEN"
    history = [(e["event_type"], e["note"]) for e in controls.period_history(fresh, "2026-07")]
    assert history == [("CLOSED", "July signed off"),
                       ("REOPENED", "restating a misposted vendor invoice")]


def test_reopening_an_open_period_is_refused(fresh):
    with pytest.raises(PermissionError):
        controls.reopen_period(fresh, "2026-08", "no need")


# ------------------------------------------------------------------ the report
def test_report_shows_the_controls(fresh, tmp_path):
    """Phase 6: the ledger work has to be visible to someone who only opens
    the workbook."""
    from openpyxl import load_workbook

    period_id = "2026-08"
    carried = cf.resolve(fresh, period_id)
    bank, gl, balances, matches, exceptions, proof = rec.reconcile(
        conn=fresh, period_id=period_id, carried=carried)
    ledger = {"aging": controls.aging(fresh, period_id),
              "aging_summary": controls.aging_summary(fresh, period_id),
              "rollforward": controls.rollforward(fresh),
              "breaks": controls.rollforward_breaks(fresh)}
    out = rec.build_report(bank, gl, matches, exceptions, proof, balances["period_end"],
                           tmp_path / "rec.xlsx", ledger=ledger)

    wb = load_workbook(out)
    assert "Open Items (aged)" in wb.sheetnames
    assert "Rollforward" in wb.sheetnames

    aged = wb["Open Items (aged)"]
    flags = [c.value for row in aged.iter_rows() for c in row]
    assert "ESCALATE" in flags and "FOLLOW_UP" in flags
    assert sum(1 for v in flags if v == "ESCALATE") == 4

    roll = [c.value for row in wb["Rollforward"].iter_rows() for c in row]
    assert roll.count("BALANCED") == 3
    assert any(str(v).startswith("No breaks") for v in roll if v)


def test_report_without_a_ledger_has_no_control_tabs(tmp_path):
    """File mode still works with no database, and simply omits them."""
    from openpyxl import load_workbook

    files = ledger_files = __import__("db").period_files("2026-08", DATA)
    bank, gl, balances, matches, exceptions, proof = rec.reconcile(
        ledger_files["bank"], files["gl"], files["balances"])
    out = rec.build_report(bank, gl, matches, exceptions, proof, balances["period_end"],
                           tmp_path / "rec.xlsx")
    wb = load_workbook(out)
    assert "Open Items (aged)" not in wb.sheetnames
    assert len(wb.sheetnames) == 7