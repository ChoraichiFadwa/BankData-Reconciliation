"""
Tests for the Power BI export.

The dashboard is only worth showing if it says exactly what the controls say.
These tests pin that: the exported tables must agree with the rollforward,
the engine's own numbers, and each other.
"""

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import carryforward as cf  # noqa: E402
import controls  # noqa: E402
import db as ledger  # noqa: E402
import export_powerbi as pbi  # noqa: E402
import reconcile as rec  # noqa: E402
import store  # noqa: E402

DATA = ROOT / "data"
PERIODS = ["2026-06", "2026-07", "2026-08"]


def run_period(conn, period_id):
    ledger.load_period(conn, period_id, DATA, replace=True)
    carried = cf.resolve(conn, period_id)
    bank, gl, _, matches, exceptions, proof = rec.reconcile(
        conn=conn, period_id=period_id, carried=carried)
    store.save_run(conn, period_id, bank, gl, matches, exceptions, proof, carried=carried)
    controls.apply_escalations(conn, period_id)


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pbi")
    db_path = ledger.init_db(tmp / "ledger.db")
    conn = ledger.connect(db_path)
    for p in PERIODS:
        run_period(conn, p)
    conn.close()
    summary = pbi.export(db_path, tmp / "out")

    def read(name):
        with open(tmp / "out" / name, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    return {"summary": summary, "db": db_path,
            "period": read("fact_period.csv"), "item": read("fact_open_item.csv"),
            "category": read("dim_category.csv"), "bucket": read("dim_bucket.csv")}


def cents(x):
    return round(float(x) * 100)


def test_one_row_per_period(exported):
    assert [r["period_id"] for r in exported["period"]] == PERIODS


def test_june_match_rate_is_the_engines(exported):
    june = exported["period"][0]
    assert (int(june["n_exact"]), int(june["n_timing"]), int(june["n_tolerance"])) == (95, 15, 8)
    assert int(june["bank_rows"]) == 127
    assert float(june["match_rate"]) == round(118 / 127, 4)
    assert june["proof_status"] == "TIES"


def test_every_period_balances(exported):
    assert all(r["rollforward_status"] == "BALANCED" for r in exported["period"])


def test_open_items_add_up_to_the_rollforward(exported):
    for p in exported["period"]:
        mine = [i for i in exported["item"] if i["as_of_period"] == p["period_id"]]
        assert len(mine) == int(p["closing_count"])
        assert sum(cents(i["amount"]) for i in mine) == cents(p["closing"])


def test_every_category_and_bucket_has_a_dimension_row(exported):
    cats = {c["category"] for c in exported["category"]}
    buckets = {b["bucket"] for b in exported["bucket"]}
    for i in exported["item"]:
        assert i["category"] in cats
        assert i["bucket"] in buckets


def test_every_category_has_an_owner(exported):
    assert {c["owner"] for c in exported["category"]} <= {"Accountant", "Controller"}


def test_a_rerun_is_not_double_counted(exported, tmp_path):
    """Reruns stay in recon_run for audit; the dashboard must use the latest."""
    conn = ledger.connect(exported["db"])
    run_period(conn, "2026-08")
    conn.close()
    pbi.export(exported["db"], tmp_path)
    with open(tmp_path / "fact_period.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    aug = [r for r in rows if r["period_id"] == "2026-08"]
    assert len(aug) == 1
    assert aug[0]["n_matched"] == exported["period"][2]["n_matched"]


def test_export_refuses_when_it_disagrees_with_the_controls(exported, tmp_path, monkeypatch):
    real = controls.aging
    monkeypatch.setattr(controls, "aging", lambda conn, p, **k: list(real(conn, p))[1:])
    with pytest.raises(SystemExit, match="disagrees with the controls"):
        pbi.export(exported["db"], tmp_path)