"""
Tests for phases 3 and 4: three chained months, and the carry-forward pass
that clears one month's items against the next month's transactions.

The behaviour these pin is the reason the project uses a database at all:
without pass 0 every carried item becomes two false exceptions in the new
period, and the proof misses by the opening gap between the bank and the GL.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import carryforward as cf  # noqa: E402
import db as ledger  # noqa: E402
import reconcile as rec  # noqa: E402
import store  # noqa: E402

DATA = ROOT / "data"
PERIODS = ["2026-06", "2026-07", "2026-08"]

# June's Vermeer check never clears; the June deposit the bank never received
# and the two unidentified June items are never resolved either.
VERMEER_CENTS = -1_589_023
STALE_DEPOSIT_CENTS = 940_658


@pytest.fixture(scope="module")
def closed_months(tmp_path_factory):
    """June, July and August reconciled in order, as a real close would run."""
    path = tmp_path_factory.mktemp("ledger") / "ledger.db"
    conn = ledger.connect(ledger.init_db(path))
    periods = {}
    for period_id in PERIODS:
        ledger.load_period(conn, period_id, DATA)
        carried = cf.resolve(conn, period_id)
        bank, gl, balances, matches, exceptions, proof = rec.reconcile(
            conn=conn, period_id=period_id, carried=carried)
        run_id = store.save_run(conn, period_id, bank, gl, matches, exceptions, proof,
                                carried=carried)
        periods[period_id] = {"carried": carried, "matches": matches,
                              "exceptions": exceptions, "proof": proof, "run_id": run_id}
    yield {"conn": conn, "periods": periods}
    conn.close()


def items_of(result, period_id, **where):
    rows = result["conn"].execute("""
        SELECT i.*, COALESCE(b.description, g.memo) AS text
        FROM reconciling_item i
        LEFT JOIN bank_txn b ON b.bank_txn_id = i.bank_txn_id
        LEFT JOIN gl_entry g ON g.gl_entry_id = i.gl_entry_id
        WHERE i.origin_period_id = ?""", (period_id,)).fetchall()
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


# ------------------------------------------------------------------ phase 3: the data
def test_every_period_loads(closed_months):
    rows = {r["period_id"]: r for r in ledger.status(closed_months["conn"])}
    assert list(rows) == PERIODS
    assert (rows["2026-06"]["bank_rows"], rows["2026-06"]["gl_rows"]) == (127, 127)
    assert all(rows[p]["runs"] == 1 for p in PERIODS)


def test_periods_chain_balance_to_balance(closed_months):
    """Each period opens where the last one closed, per side."""
    rows = closed_months["conn"].execute(
        "SELECT * FROM recon_period ORDER BY period_id").fetchall()
    for prev, nxt in zip(rows, rows[1:]):
        assert nxt["bank_open_cents"] == prev["bank_close_cents"]
        assert nxt["gl_open_cents"] == prev["gl_close_cents"]


def test_bank_and_gl_open_apart_after_june(closed_months):
    """June's outstanding items ARE the gap — the thing pass 0 has to explain."""
    jun, jul = closed_months["conn"].execute(
        "SELECT * FROM recon_period ORDER BY period_id").fetchall()[:2]
    assert jun["bank_open_cents"] == jun["gl_open_cents"]        # first period, clean start
    assert jul["bank_open_cents"] != jul["gl_open_cents"]
    assert jul["bank_open_cents"] - jul["gl_open_cents"] == -3_313_392


# ------------------------------------------------------------------ phase 4: pass 0
def test_every_period_ties(closed_months):
    for period_id in PERIODS:
        proof = closed_months["periods"][period_id]["proof"]
        assert proof["difference"] == 0, period_id
        assert proof["completeness"]["Bank"]["diff"] == 0, period_id
        assert proof["completeness"]["GL"]["diff"] == 0, period_id


def test_carryforward_counts(closed_months):
    cleared = {p: len(closed_months["periods"][p]["carried"]["resolutions"]) for p in PERIODS}
    assert cleared == {"2026-06": 0, "2026-07": 15, "2026-08": 5}


def test_open_items_roll_from_period_to_period(closed_months):
    """June raises 19, July clears 15 and raises 6, August clears 5 and raises 4."""
    open_at_end = {p: len(closed_months["periods"][p]["exceptions"]) for p in PERIODS}
    assert open_at_end == {"2026-06": 19, "2026-07": 10, "2026-08": 9}


def test_july_without_pass_0_is_wrong(closed_months, tmp_path):
    """The control: the same July, reconciled with no memory of June."""
    files = ledger.period_files("2026-07", DATA)
    *_, exceptions, proof = rec.reconcile(files["bank"], files["gl"], files["balances"])
    assert len(exceptions) > len(closed_months["periods"]["2026-07"]["exceptions"])
    assert proof["difference"] == -3_313_392        # exactly the opening gap
    # the June items reappear as brand-new mysteries
    unidentified = [e for e in exceptions if e["Category"].startswith("UNID")]
    assert len(unidentified) >= 6


def test_late_check_clears_in_the_next_period(closed_months):
    """A check written Jun 26 and cleared Jul 2: 6 days, but across a period
    boundary, so no window in July could ever reach it."""
    item = items_of(closed_months, "2026-06", amount_cents=-741_266)[0]
    assert item["status"] == "CLEARED"
    assert item["resolved_period_id"] == "2026-07"
    cleared_by = closed_months["conn"].execute(
        "SELECT * FROM bank_txn WHERE bank_txn_id = ?", (item["cleared_bank_txn_id"],)).fetchone()
    assert cleared_by["period_id"] == "2026-07"
    assert cleared_by["check_no"] == "1079"


def test_deposits_in_transit_clear_at_the_bank(closed_months):
    for cents in (4_831_077, 1_276_431):
        item = items_of(closed_months, "2026-06", amount_cents=cents)[0]
        assert (item["category"], item["status"]) == ("DIT", "CLEARED")
        assert item["cleared_bank_txn_id"] is not None


def test_bank_side_items_clear_when_the_gl_books_them(closed_months):
    """The bookkeeper posts the JEs the June exception report asked for."""
    for cents in (-12_500, -4_500, -38_944, 21_487, -61_208, -387_540):
        item = items_of(closed_months, "2026-06", amount_cents=cents)[0]
        assert item["status"] == "CLEARED", cents
        assert item["cleared_gl_entry_id"] is not None
        assert item["cleared_bank_txn_id"] is None


def test_duplicate_clears_only_by_reversal(closed_months):
    item = items_of(closed_months, "2026-06", category="DUPLICATE")[0]
    reversal = closed_months["conn"].execute(
        "SELECT * FROM gl_entry WHERE gl_entry_id = ?", (item["cleared_gl_entry_id"],)).fetchone()
    assert item["status"] == "CLEARED"
    assert reversal["amount_cents"] == -item["amount_cents"]      # opposite sign
    assert "Reversal" in reversal["memo"]


def test_residual_clears_when_written_off(closed_months):
    """The cents pass 3 left over stay on the rec until the JE is booked."""
    jun = items_of(closed_months, "2026-06", category="RESIDUAL")[0]
    assert (jun["side"], jun["amount_cents"], jun["status"]) == ("RESIDUAL", -35, "CLEARED")
    assert jun["resolved_period_id"] == "2026-07"
    aug = items_of(closed_months, "2026-08", category="RESIDUAL")[0]
    assert aug["status"] == "OPEN"                                # nothing booked it yet


def test_items_that_never_clear_stay_open_and_age(closed_months):
    vermeer = items_of(closed_months, "2026-06", amount_cents=VERMEER_CENTS)[0]
    stale = items_of(closed_months, "2026-06", amount_cents=STALE_DEPOSIT_CENTS)[0]
    assert vermeer["status"] == stale["status"] == "OPEN"

    august = {e["cents"]: e for e in closed_months["periods"]["2026-08"]["exceptions"]}
    assert august[VERMEER_CENTS]["Age (days)"] == 63              # past a 60-day follow-up
    assert august[STALE_DEPOSIT_CENTS]["Age (days)"] == 80
    assert august[VERMEER_CENTS]["Origin period"] == "2026-06"


def test_carried_items_appear_on_the_later_rec(closed_months):
    """A June check is still a reconciling item on July's and August's recs."""
    for period_id in ("2026-07", "2026-08"):
        carried = [e for e in closed_months["periods"][period_id]["exceptions"]
                   if e["Origin period"] < period_id]
        assert any(e["cents"] == VERMEER_CENTS for e in carried)
        assert all("carried forward" in e["Probable cause"] for e in carried)


def test_consumed_rows_are_not_matched_again(closed_months):
    """A July bank row that cleared a June item must not also pair with a July
    GL row, and must not become a July exception."""
    carried = closed_months["periods"]["2026-07"]["carried"]
    assert len(carried["consumed_bank"]) + len(carried["consumed_gl"]) == 15

    conn = closed_months["conn"]
    matched_bank = {r["bank_txn_id"] for r in conn.execute("SELECT bank_txn_id FROM match")}
    matched_gl = {r["gl_entry_id"] for r in conn.execute("SELECT gl_entry_id FROM match")}
    assert not (carried["consumed_bank"] & matched_bank)
    assert not (carried["consumed_gl"] & matched_gl)

    # and they are not reported as July's own problems either
    july_bank_rows = {e["Source row"] for e in closed_months["periods"]["2026-07"]["exceptions"]
                      if e["Side"] == "Bank only" and e["Origin period"] == "2026-07"}
    placeholders = ",".join("?" * len(carried["consumed_bank"]))
    consumed_rows = {r["source_row"] for r in conn.execute(
        f"SELECT source_row FROM bank_txn WHERE bank_txn_id IN ({placeholders})",
        tuple(carried["consumed_bank"]))}
    assert not (consumed_rows & july_bank_rows)


def test_every_item_has_a_full_history(closed_months):
    """CREATED when raised, CLEARED when resolved — nothing edited in place."""
    rows = closed_months["conn"].execute("""
        SELECT i.item_id, i.status,
               SUM(e.event_type = 'CREATED') AS created,
               SUM(e.event_type = 'CLEARED') AS cleared
        FROM reconciling_item i JOIN item_event e USING (item_id)
        GROUP BY i.item_id""").fetchall()
    assert rows
    for r in rows:
        assert r["created"] == 1
        assert r["cleared"] == (1 if r["status"] == "CLEARED" else 0)


def test_clearing_records_which_period_resolved_it(closed_months):
    rows = closed_months["conn"].execute("""
        SELECT origin_period_id, resolved_period_id, COUNT(*) n
        FROM reconciling_item WHERE status = 'CLEARED'
        GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
    assert [(r["origin_period_id"], r["resolved_period_id"], r["n"]) for r in rows] == [
        ("2026-06", "2026-07", 15),
        ("2026-07", "2026-08", 5)]


# ------------------------------------------------------------------ reruns and write-offs
def test_rerunning_a_period_takes_back_its_clearings(tmp_path):
    """July can be re-run while it is open: June's items must not stay cleared
    by a run that no longer exists."""
    conn = ledger.connect(ledger.init_db(tmp_path / "ledger.db"))
    for period_id in ("2026-06", "2026-07"):
        ledger.load_period(conn, period_id, DATA)
        carried = cf.resolve(conn, period_id)
        bank, gl, _, matches, exceptions, proof = rec.reconcile(
            conn=conn, period_id=period_id, carried=carried)
        store.save_run(conn, period_id, bank, gl, matches, exceptions, proof, carried=carried)

    before = conn.execute("SELECT COUNT(*) FROM reconciling_item WHERE status = 'CLEARED'"
                          ).fetchone()[0]
    carried = cf.resolve(conn, "2026-07")
    bank, gl, _, matches, exceptions, proof = rec.reconcile(
        conn=conn, period_id="2026-07", carried=carried)
    store.save_run(conn, "2026-07", bank, gl, matches, exceptions, proof, carried=carried)

    assert conn.execute("SELECT COUNT(*) FROM reconciling_item WHERE status = 'CLEARED'"
                        ).fetchone()[0] == before == 15
    assert conn.execute("SELECT COUNT(*) FROM reconciling_item").fetchone()[0] == 19 + 6
    assert proof["difference"] == 0
    conn.close()


def test_write_off_closes_an_item_by_decision(closed_months):
    """Some items never clear: an accountant writes them off, and the ledger
    records who decided what, and when."""
    conn = closed_months["conn"]
    item = items_of(closed_months, "2026-06", amount_cents=STALE_DEPOSIT_CENTS)[0]
    store.write_off(conn, item["item_id"], "2026-08", "Deposit never made — written off at close")

    after = conn.execute("SELECT * FROM reconciling_item WHERE item_id = ?",
                         (item["item_id"],)).fetchone()
    assert (after["status"], after["resolved_period_id"]) == ("WRITTEN_OFF", "2026-08")
    assert conn.execute("SELECT COUNT(*) FROM item_event WHERE item_id = ? "
                        "AND event_type = 'WRITTEN_OFF'", (item["item_id"],)).fetchone()[0] == 1
    with pytest.raises(LookupError):
        store.write_off(conn, item["item_id"], "2026-08", "again")