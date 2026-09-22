"""
Tests for the ledger (phase 1): loading, idempotency, and the constraints that
make the database — not the Python — responsible for the accounting invariants.

The engine is untouched in this phase, so tests/test_reconcile.py must keep
passing unchanged.
"""

import csv
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db as ledger  # noqa: E402
import reconcile as rec  # noqa: E402

PERIOD = "2026-06"
DATA = ROOT / "data"


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(ledger.init_db(tmp_path / "ledger.db"))
    yield c
    c.close()


@pytest.fixture
def loaded(conn):
    ledger.load_period(conn, PERIOD, DATA)
    return conn


def counts(conn):
    n = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    return n("bank_txn"), n("gl_entry")


def copy_data(tmp_path):
    """A writable copy of data/, so tests can modify the inputs."""
    out = tmp_path / "data"
    out.mkdir()
    for p in ledger.period_files(PERIOD, DATA).values():
        (out / p.name).write_bytes(p.read_bytes())
    return out


# ------------------------------------------------------------------ loading
def test_init_creates_every_table(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"recon_period", "bank_txn", "gl_entry", "recon_run",
            "match", "reconciling_item", "item_event"} <= tables


def test_load_stores_every_row(loaded):
    assert counts(loaded) == (127, 127)
    period = loaded.execute("SELECT * FROM recon_period").fetchone()
    assert period["period_id"] == PERIOD
    assert period["status"] == "OPEN"
    assert period["bank_open_cents"] == period["gl_open_cents"] == 18_435_219


def test_reload_is_a_no_op(loaded):
    result = ledger.load_period(loaded, PERIOD, DATA)
    assert result["action"] == "unchanged"
    assert counts(loaded) == (127, 127)


def test_changed_data_is_refused_without_replace(conn, tmp_path):
    data = copy_data(tmp_path)
    ledger.load_period(conn, PERIOD, data)
    bank = data / ledger.period_files(PERIOD, data)["bank"].name
    rows = list(csv.reader(open(bank, newline="", encoding="utf-8")))
    rows[5][1] = "EDITED BY HAND"
    with open(bank, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)

    with pytest.raises(ValueError, match="--replace"):
        ledger.load_period(conn, PERIOD, data)
    assert counts(conn) == (127, 127)                     # nothing half-written

    ledger.load_period(conn, PERIOD, data, replace=True)
    assert counts(conn) == (127, 127)
    assert conn.execute("SELECT COUNT(*) FROM bank_txn WHERE description = 'EDITED BY HAND'"
                        ).fetchone()[0] == 1


def test_duplicate_source_line_is_kept(loaded):
    """The planted duplicate is identical to another line in every field.
    Keying rows by content alone would silently drop it — the exact break the
    engine exists to catch."""
    rows = loaded.execute(
        "SELECT * FROM gl_entry WHERE doc_no = 'CHQ#1042' ORDER BY source_row").fetchall()
    assert len(rows) == 2
    assert rows[0]["amount_cents"] == rows[1]["amount_cents"] == -1_023_018
    assert rows[0]["row_hash"] != rows[1]["row_hash"]     # differ only by line number


def test_amounts_are_exact_cents(loaded):
    for file_key, table, col in (("bank", "bank_txn", "Amount"), ("gl", "gl_entry", "Amount")):
        path = ledger.period_files(PERIOD, DATA)[file_key]
        expected = sum(rec.to_cents(r[col]) for r in ledger.read_csv_rows(path))
        stored = loaded.execute(f"SELECT SUM(amount_cents) FROM {table}").fetchone()[0]
        assert stored == expected


def test_check_numbers_are_extracted(loaded):
    assert loaded.execute(
        "SELECT check_no FROM gl_entry WHERE doc_no = 'CHQ#1042' LIMIT 1").fetchone()[0] == "1042"
    assert loaded.execute(
        "SELECT check_no FROM bank_txn WHERE reference LIKE 'REF%' LIMIT 1").fetchone()[0] == ""


def test_closed_period_refuses_a_load(loaded):
    loaded.execute("UPDATE recon_period SET status = 'CLOSED', closed_at = '2026-07-05' "
                   "WHERE period_id = ?", (PERIOD,))
    with pytest.raises(PermissionError):
        ledger.load_period(loaded, PERIOD, DATA)


# ------------------------------------------------------------------ constraints
def make_run(conn):
    cur = conn.execute("""INSERT INTO recon_run (period_id, run_at, engine_version, params_json)
                          VALUES (?, '2026-07-01T09:00:00', 'test', '{}')""", (PERIOD,))
    return cur.lastrowid


def test_foreign_keys_are_enforced(loaded):
    run_id = make_run(loaded)
    with pytest.raises(sqlite3.IntegrityError):
        loaded.execute("""INSERT INTO match (run_id, pass, method, bank_txn_id, gl_entry_id,
                                             date_delta_days, cents_delta)
                          VALUES (?, 'EXACT', 'amount + date', 999999, 1, 0, 0)""", (run_id,))


def test_a_transaction_cannot_be_matched_twice(loaded):
    """One-to-one matching, enforced by the database rather than trusted to Python."""
    run_id = make_run(loaded)
    insert = """INSERT INTO match (run_id, pass, method, bank_txn_id, gl_entry_id,
                                   date_delta_days, cents_delta)
                VALUES (?, 'EXACT', 'amount + date', ?, ?, 0, 0)"""
    loaded.execute(insert, (run_id, 1, 1))
    with pytest.raises(sqlite3.IntegrityError):
        loaded.execute(insert, (run_id, 1, 2))           # bank row reused
    with pytest.raises(sqlite3.IntegrityError):
        loaded.execute(insert, (run_id, 2, 1))           # GL row reused


def test_unknown_pass_is_rejected(loaded):
    run_id = make_run(loaded)
    with pytest.raises(sqlite3.IntegrityError):
        loaded.execute("""INSERT INTO match (run_id, pass, method, bank_txn_id, gl_entry_id,
                                             date_delta_days, cents_delta)
                          VALUES (?, 'GUESSWORK', 'vibes', 1, 1, 0, 0)""", (run_id,))


def test_item_must_sit_on_exactly_one_side(loaded):
    run_id = make_run(loaded)
    item = """INSERT INTO reconciling_item (origin_period_id, origin_run_id, side,
                                            bank_txn_id, gl_entry_id, category, amount_cents)
              VALUES (?, ?, ?, ?, ?, 'DIT', 100)"""
    with pytest.raises(sqlite3.IntegrityError):          # both sides
        loaded.execute(item, (PERIOD, run_id, "BANK", 1, 1))
    with pytest.raises(sqlite3.IntegrityError):          # neither side
        loaded.execute(item, (PERIOD, run_id, "BANK", None, None))
    with pytest.raises(sqlite3.IntegrityError):          # side contradicts the row
        loaded.execute(item, (PERIOD, run_id, "GL", 1, None))
    loaded.execute(item, (PERIOD, run_id, "BANK", 1, None))


def test_resolved_status_and_period_stay_consistent(loaded):
    """Guards the rollforward: opening + new − cleared − written off = closing."""
    run_id = make_run(loaded)
    loaded.execute("""INSERT INTO reconciling_item (origin_period_id, origin_run_id, side,
                                                    gl_entry_id, category, amount_cents)
                      VALUES (?, ?, 'GL', 1, 'OS_CHECK', -500)""", (PERIOD, run_id))
    with pytest.raises(sqlite3.IntegrityError):          # cleared into nowhere
        loaded.execute("UPDATE reconciling_item SET status = 'CLEARED' WHERE item_id = 1")
    with pytest.raises(sqlite3.IntegrityError):          # open, yet resolved somewhere
        loaded.execute("UPDATE reconciling_item SET resolved_period_id = ? WHERE item_id = 1",
                       (PERIOD,))


def test_deleting_a_period_takes_its_rows(loaded):
    loaded.execute("DELETE FROM recon_period WHERE period_id = ?", (PERIOD,))
    assert counts(loaded) == (0, 0)


# ------------------------------------------------------------------ parity with the CSV loader
def test_stored_period_matches_the_csv_loader(loaded):
    """Phase 2 swaps reconcile.load() for db.period_frames(): same data, same shape."""
    files = ledger.period_files(PERIOD, DATA)
    csv_bank, csv_gl = rec.load(files["bank"], files["gl"])
    db_bank, db_gl, db_balances = ledger.period_frames(loaded, PERIOD)

    for csv_df, db_df in ((csv_bank, db_bank), (csv_gl, db_gl)):
        assert list(db_df["cents"]) == list(csv_df["cents"])
        assert list(db_df["Date"]) == list(csv_df["Date"])
        assert list(db_df["chk"]) == list(csv_df["chk"])
        assert list(db_df["src_row"]) == list(csv_df["src_row"])

    csv_balances = rec.load_balances(files["balances"])
    for side in ("BANK", "GL"):
        assert db_balances[side] == csv_balances[side]
    assert db_balances["period_end"] == csv_balances["period_end"]