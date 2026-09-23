"""
Tests for phase 2: the engine reads a period from the ledger and writes its
run back into it.

The behaviour must not change in this phase, so the safety net here is parity:
reconciling from the ledger and reconciling from the CSVs must produce exactly
the same matches and exceptions. tests/test_reconcile.py stays unchanged and
green alongside these.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db as ledger  # noqa: E402
import reconcile as rec  # noqa: E402
import store  # noqa: E402

PERIOD = "2026-06"
DATA = ROOT / "data"


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(ledger.init_db(tmp_path / "ledger.db"))
    ledger.load_period(c, PERIOD, DATA)
    yield c
    c.close()


@pytest.fixture
def saved(conn):
    bank, gl, balances, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    run_id = store.save_run(conn, PERIOD, bank, gl, matches, exceptions, proof)
    return {"conn": conn, "run_id": run_id, "matches": matches, "exceptions": exceptions}


def one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


# ------------------------------------------------------------------ reading from the ledger
def test_ledger_and_csv_reconcile_identically(conn):
    files = ledger.period_files(PERIOD, DATA)
    _, _, _, csv_matches, csv_exc, csv_proof = rec.reconcile(
        files["bank"], files["gl"], files["balances"])
    _, _, _, db_matches, db_exc, db_proof = rec.reconcile(conn=conn, period_id=PERIOD)

    key = lambda m: (m["pass"], m["method"], m["bank_idx"], m["gl_idx"],  # noqa: E731
                     m["date_delta"], m["cents_delta"], m["score"])
    assert [key(m) for m in db_matches] == [key(m) for m in csv_matches]
    assert [(e["Side"], e["cents"], e["Category"], e["Source row"]) for e in db_exc] == \
           [(e["Side"], e["cents"], e["Category"], e["Source row"]) for e in csv_exc]
    assert db_proof == csv_proof


def test_golden_numbers_survive_the_ledger(conn):
    _, _, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    assert len(matches) == 118
    assert len(exceptions) == 18
    assert proof["difference"] == 0


def test_csv_frames_cannot_be_saved(conn):
    """Saving needs database ids: frames read from CSVs don't have them."""
    files = ledger.period_files(PERIOD, DATA)
    bank, gl, _, matches, exceptions, proof = rec.reconcile(
        files["bank"], files["gl"], files["balances"])
    with pytest.raises(ValueError, match="ledger"):
        store.save_run(conn, PERIOD, bank, gl, matches, exceptions, proof)


# ------------------------------------------------------------------ what gets stored
def test_run_row_records_the_result(saved):
    run = store.run_summary(saved["conn"], PERIOD)
    assert (run["n_exact"], run["n_timing"], run["n_tolerance"]) == (95, 15, 8)
    assert run["n_carryforward"] == 0                     # pass 0 arrives in phase 4
    assert run["n_exceptions"] == 18
    assert run["proof_diff_cents"] == 0
    assert run["bank_control_cents"] == run["gl_control_cents"] == 0
    assert run["n_matches"] == 118 and run["n_open"] == 18


def test_run_records_the_thresholds_it_used(saved):
    import json
    params = json.loads(store.run_summary(saved["conn"], PERIOD)["params_json"])
    assert params["timing_window_days"] == rec.TIMING_WINDOW_DAYS
    assert params["fuzzy_threshold"] == rec.FUZZY_THRESHOLD


def test_matches_are_stored_with_pass_and_method(saved):
    rows = saved["conn"].execute(
        "SELECT pass, method, COUNT(*) n FROM match GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    assert [(r["pass"], r["method"], r["n"]) for r in rows] == [
        ("EXACT", "amount + date", 95),
        ("TIMING", "check no.", 13),
        ("TIMING", "date window", 2),
        ("TOLERANCE", "fuzzy", 8)]


def test_matches_point_at_the_right_transactions(saved):
    """Spot-check the id translation: the late check pair, stored end to end."""
    row = saved["conn"].execute("""
        SELECT m.method, m.date_delta_days, b.description, g.memo, b.amount_cents
        FROM match m JOIN bank_txn b USING (bank_txn_id) JOIN gl_entry g USING (gl_entry_id)
        WHERE g.memo LIKE 'Telecon Design — survey%'""").fetchone()
    assert (row["method"], row["date_delta_days"]) == ("check no.", 11)
    assert row["amount_cents"] == -561_238


def test_items_are_stored_open_with_their_categories(saved):
    conn = saved["conn"]
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item") == 18
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item WHERE status = 'OPEN'") == 18
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item WHERE resolved_period_id IS NOT NULL") == 0
    stored = conn.execute("""SELECT side, amount_cents, category FROM reconciling_item
                             ORDER BY ABS(amount_cents) DESC""").fetchall()
    expected = [("BANK" if e["Side"] == "Bank only" else "GL", e["cents"], e["Category"])
                for e in saved["exceptions"]]
    assert [(r["side"], r["amount_cents"], r["category"]) for r in stored] == expected


def test_every_item_has_one_created_event(saved):
    conn = saved["conn"]
    assert one(conn, "SELECT COUNT(*) FROM item_event WHERE event_type = 'CREATED'") == 18
    assert one(conn, """SELECT COUNT(*) FROM (SELECT item_id FROM item_event
                        GROUP BY item_id HAVING COUNT(*) <> 1)""") == 0


def test_items_link_back_to_their_source_row(saved):
    """The $48K deposit in transit is item, source row and GL entry, all the same thing."""
    row = saved["conn"].execute("""
        SELECT i.category, i.amount_cents, g.memo, g.txn_date, g.source_row
        FROM reconciling_item i JOIN gl_entry g USING (gl_entry_id)
        WHERE i.amount_cents = 4831077""").fetchone()
    assert row["category"] == "DIT"
    assert row["txn_date"] == "2026-06-30"
    assert row["memo"].startswith("Rogers Communications")


# ------------------------------------------------------------------ reruns and failures
def test_rerunning_supersedes_the_previous_run(saved):
    conn = saved["conn"]
    bank, gl, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    second = store.save_run(conn, PERIOD, bank, gl, matches, exceptions, proof)

    assert second > saved["run_id"]                       # run numbers are never reused
    assert one(conn, "SELECT COUNT(*) FROM recon_run") == 1
    assert one(conn, "SELECT COUNT(*) FROM match") == 118
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item") == 18
    assert one(conn, "SELECT COUNT(*) FROM item_event") == 18


def test_a_failed_save_leaves_nothing_behind(conn, monkeypatch):
    """All or nothing: a half-written month would poison every later number."""
    bank, gl, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    broken = [dict(e) for e in exceptions]
    broken[7]["Source row"] = 999999                      # no such row: blows up mid-write

    with pytest.raises(KeyError):
        store.save_run(conn, PERIOD, bank, gl, matches, broken, proof)
    assert one(conn, "SELECT COUNT(*) FROM recon_run") == 0
    assert one(conn, "SELECT COUNT(*) FROM match") == 0
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item") == 0


def test_closed_period_cannot_be_rerun(saved):
    conn = saved["conn"]
    conn.execute("UPDATE recon_period SET status = 'CLOSED', closed_at = '2026-07-05' "
                 "WHERE period_id = ?", (PERIOD,))
    bank, gl, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    with pytest.raises(PermissionError):
        store.save_run(conn, PERIOD, bank, gl, matches, exceptions, proof)


def test_unknown_period_is_refused(conn):
    bank, gl, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    with pytest.raises(LookupError):
        store.save_run(conn, "2026-07", bank, gl, matches, exceptions, proof)


def test_deleting_a_run_takes_its_matches_and_items(saved):
    conn = saved["conn"]
    conn.execute("DELETE FROM recon_run WHERE run_id = ?", (saved["run_id"],))
    assert one(conn, "SELECT COUNT(*) FROM match") == 0
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item") == 0
    assert one(conn, "SELECT COUNT(*) FROM item_event") == 0
    assert one(conn, "SELECT COUNT(*) FROM bank_txn") == 127     # transactions survive


def test_items_cannot_outlive_their_transactions(saved):
    with pytest.raises(sqlite3.IntegrityError):"""
Tests for phase 2: the engine reads a period from the ledger and writes its
run back into it.

The behaviour must not change in this phase, so the safety net here is parity:
reconciling from the ledger and reconciling from the CSVs must produce exactly
the same matches and exceptions. tests/test_reconcile.py stays unchanged and
green alongside these.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db as ledger  # noqa: E402
import reconcile as rec  # noqa: E402
import store  # noqa: E402

PERIOD = "2026-06"
DATA = ROOT / "data"


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(ledger.init_db(tmp_path / "ledger.db"))
    ledger.load_period(c, PERIOD, DATA)
    yield c
    c.close()


@pytest.fixture
def saved(conn):
    bank, gl, balances, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    run_id = store.save_run(conn, PERIOD, bank, gl, matches, exceptions, proof)
    return {"conn": conn, "run_id": run_id, "matches": matches, "exceptions": exceptions}


def one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


# ------------------------------------------------------------------ reading from the ledger
def test_ledger_and_csv_reconcile_identically(conn):
    files = ledger.period_files(PERIOD, DATA)
    _, _, _, csv_matches, csv_exc, csv_proof = rec.reconcile(
        files["bank"], files["gl"], files["balances"])
    _, _, _, db_matches, db_exc, db_proof = rec.reconcile(conn=conn, period_id=PERIOD)

    key = lambda m: (m["pass"], m["method"], m["bank_idx"], m["gl_idx"],  # noqa: E731
                     m["date_delta"], m["cents_delta"], m["score"])
    assert [key(m) for m in db_matches] == [key(m) for m in csv_matches]
    assert [(e["Side"], e["cents"], e["Category"], e["Source row"]) for e in db_exc] == \
           [(e["Side"], e["cents"], e["Category"], e["Source row"]) for e in csv_exc]
    assert db_proof == csv_proof


def test_golden_numbers_survive_the_ledger(conn):
    _, _, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    assert len(matches) == 118
    assert len(exceptions) == 19
    assert proof["difference"] == 0


def test_csv_frames_cannot_be_saved(conn):
    """Saving needs database ids: frames read from CSVs don't have them."""
    files = ledger.period_files(PERIOD, DATA)
    bank, gl, _, matches, exceptions, proof = rec.reconcile(
        files["bank"], files["gl"], files["balances"])
    with pytest.raises(ValueError, match="ledger"):
        store.save_run(conn, PERIOD, bank, gl, matches, exceptions, proof)


# ------------------------------------------------------------------ what gets stored
def test_run_row_records_the_result(saved):
    run = store.run_summary(saved["conn"], PERIOD)
    assert (run["n_exact"], run["n_timing"], run["n_tolerance"]) == (95, 15, 8)
    assert run["n_carryforward"] == 0                     # pass 0 arrives in phase 4
    assert run["n_exceptions"] == 19
    assert run["proof_diff_cents"] == 0
    assert run["bank_control_cents"] == run["gl_control_cents"] == 0
    assert run["n_matches"] == 118 and run["n_open"] == 19


def test_run_records_the_thresholds_it_used(saved):
    import json
    params = json.loads(store.run_summary(saved["conn"], PERIOD)["params_json"])
    assert params["timing_window_days"] == rec.TIMING_WINDOW_DAYS
    assert params["fuzzy_threshold"] == rec.FUZZY_THRESHOLD


def test_matches_are_stored_with_pass_and_method(saved):
    rows = saved["conn"].execute(
        "SELECT pass, method, COUNT(*) n FROM match GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    assert [(r["pass"], r["method"], r["n"]) for r in rows] == [
        ("EXACT", "amount + date", 95),
        ("TIMING", "check no.", 13),
        ("TIMING", "date window", 2),
        ("TOLERANCE", "fuzzy", 8)]


def test_matches_point_at_the_right_transactions(saved):
    """Spot-check the id translation: the late check pair, stored end to end."""
    row = saved["conn"].execute("""
        SELECT m.method, m.date_delta_days, b.description, g.memo, b.amount_cents
        FROM match m JOIN bank_txn b USING (bank_txn_id) JOIN gl_entry g USING (gl_entry_id)
        WHERE g.memo LIKE 'Telecon Design — survey%'""").fetchone()
    assert (row["method"], row["date_delta_days"]) == ("check no.", 11)
    assert row["amount_cents"] == -561_238


def test_items_are_stored_open_with_their_categories(saved):
    conn = saved["conn"]
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item") == 19
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item WHERE status = 'OPEN'") == 19
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item WHERE resolved_period_id IS NOT NULL") == 0
    stored = conn.execute("""SELECT side, amount_cents, category FROM reconciling_item
                             ORDER BY ABS(amount_cents) DESC""").fetchall()
    sides = {"Bank only": "BANK", "GL only": "GL", "Residual": "RESIDUAL"}
    expected = [(sides[e["Side"]], e["cents"], e["Category"]) for e in saved["exceptions"]]
    assert [(r["side"], r["amount_cents"], r["category"]) for r in stored] == expected


def test_every_item_has_one_created_event(saved):
    conn = saved["conn"]
    assert one(conn, "SELECT COUNT(*) FROM item_event WHERE event_type = 'CREATED'") == 19
    assert one(conn, """SELECT COUNT(*) FROM (SELECT item_id FROM item_event
                        GROUP BY item_id HAVING COUNT(*) <> 1)""") == 0


def test_items_link_back_to_their_source_row(saved):
    """The $48K deposit in transit is item, source row and GL entry, all the same thing."""
    row = saved["conn"].execute("""
        SELECT i.category, i.amount_cents, g.memo, g.txn_date, g.source_row
        FROM reconciling_item i JOIN gl_entry g USING (gl_entry_id)
        WHERE i.amount_cents = 4831077""").fetchone()
    assert row["category"] == "DIT"
    assert row["txn_date"] == "2026-06-30"
    assert row["memo"].startswith("Rogers Communications")


# ------------------------------------------------------------------ reruns and failures
def test_rerunning_supersedes_the_previous_run(saved):
    conn = saved["conn"]
    bank, gl, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    second = store.save_run(conn, PERIOD, bank, gl, matches, exceptions, proof)

    assert second > saved["run_id"]                       # run numbers are never reused
    assert one(conn, "SELECT COUNT(*) FROM recon_run") == 1
    assert one(conn, "SELECT COUNT(*) FROM match") == 118
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item") == 19
    assert one(conn, "SELECT COUNT(*) FROM item_event") == 19


def test_a_failed_save_leaves_nothing_behind(conn, monkeypatch):
    """All or nothing: a half-written month would poison every later number."""
    bank, gl, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    broken = [dict(e) for e in exceptions]
    broken[7]["Source row"] = 999999                      # no such row: blows up mid-write

    with pytest.raises(KeyError):
        store.save_run(conn, PERIOD, bank, gl, matches, broken, proof)
    assert one(conn, "SELECT COUNT(*) FROM recon_run") == 0
    assert one(conn, "SELECT COUNT(*) FROM match") == 0
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item") == 0


def test_closed_period_cannot_be_rerun(saved):
    conn = saved["conn"]
    conn.execute("UPDATE recon_period SET status = 'CLOSED', closed_at = '2026-07-05' "
                 "WHERE period_id = ?", (PERIOD,))
    bank, gl, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    with pytest.raises(PermissionError):
        store.save_run(conn, PERIOD, bank, gl, matches, exceptions, proof)


def test_unknown_period_is_refused(conn):
    bank, gl, _, matches, exceptions, proof = rec.reconcile(conn=conn, period_id=PERIOD)
    with pytest.raises(LookupError):
        store.save_run(conn, "2026-07", bank, gl, matches, exceptions, proof)


def test_deleting_a_run_takes_its_matches_and_items(saved):
    conn = saved["conn"]
    conn.execute("DELETE FROM recon_run WHERE run_id = ?", (saved["run_id"],))
    assert one(conn, "SELECT COUNT(*) FROM match") == 0
    assert one(conn, "SELECT COUNT(*) FROM reconciling_item") == 0
    assert one(conn, "SELECT COUNT(*) FROM item_event") == 0
    assert one(conn, "SELECT COUNT(*) FROM bank_txn") == 127     # transactions survive


def test_items_cannot_outlive_their_transactions(saved):
    with pytest.raises(sqlite3.IntegrityError):
        saved["conn"].execute("""INSERT INTO reconciling_item
            (origin_period_id, origin_run_id, side, bank_txn_id, category, amount_cents)
            VALUES (?, ?, 'BANK', 999999, 'DIT', 100)""", (PERIOD, saved["run_id"]))
        saved["conn"].execute("""INSERT INTO reconciling_item
            (origin_period_id, origin_run_id, side, bank_txn_id, category, amount_cents)
            VALUES (?, ?, 'BANK', 999999, 'DIT', 100)""", (PERIOD, saved["run_id"]))