"""
store.py — write a reconciliation run into the ledger.

This is the step that turns the engine from a function into something with a
memory. After it runs, the database holds what was matched, what was left over,
and every exception as an OPEN reconciling item that later periods can clear.

Everything happens in one transaction: a run lands whole or not at all. A
half-written month would be worse than no month, because every later number —
the rollforward, the aging — is built on what is stored here.

Rerunning a period supersedes that period's previous run. Runs accumulate
across periods, never within one, so reconciling June twice leaves 18 open
items, not 36.
"""

import json
from datetime import datetime, timezone

import reconcile as rec

ENGINE_VERSION = "1.1.0"


def current_params() -> dict:
    """The thresholds this run used — stored so a rerun can be compared to it."""
    return {
        "timing_window_days": rec.TIMING_WINDOW_DAYS,
        "tolerance_dollars": rec.TOLERANCE_DOLLARS,
        "tolerance_window_days": rec.TOLERANCE_WINDOW_DAYS,
        "fuzzy_threshold": rec.FUZZY_THRESHOLD,
        "month_end_days": rec.MONTH_END_DAYS,
    }


def _id_maps(bank, gl):
    """Engine row positions and source rows -> database ids."""
    if "bank_txn_id" not in bank.columns or "gl_entry_id" not in gl.columns:
        raise ValueError("frames came from CSVs, not the ledger — load the period first")
    return ({int(i): int(v) for i, v in bank["bank_txn_id"].items()},
            {int(i): int(v) for i, v in gl["gl_entry_id"].items()},
            {int(r): int(i) for r, i in zip(bank["src_row"], bank["bank_txn_id"])},
            {int(r): int(i) for r, i in zip(gl["src_row"], gl["gl_entry_id"])})


def save_run(conn, period_id, bank, gl, matches, exceptions, proof, carried=None,
             engine_version=ENGINE_VERSION, params=None) -> int:
    """Persist one run, including pass 0's resolutions. Returns the new run_id.

    Rerunning a period first takes back what its previous run did — both the
    items it raised (cascade) and the older items it cleared (reopened here) —
    so a rerun is a replacement, never an addition."""
    period = conn.execute("SELECT status FROM recon_period WHERE period_id = ?",
                          (period_id,)).fetchone()
    if period is None:
        raise LookupError(f"period {period_id} is not in the ledger")
    if period["status"] == "CLOSED":
        raise PermissionError(f"period {period_id} is closed — it cannot be re-run")

    bank_id, gl_id, bank_by_row, gl_by_row = _id_maps(bank, gl)
    counts = {"CARRYFWD": 0, "EXACT": 0, "TIMING": 0, "TOLERANCE": 0}
    for m in matches:
        counts[m["pass"].upper()] += 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    carried = carried or {"resolutions": [], "still_open": []}
    new_items = [e for e in exceptions if e.get("Origin period", period_id) == period_id]

    with conn:                                   # one transaction, all or nothing
        # reopen anything the previous run of this period cleared ...
        conn.execute("""
            UPDATE reconciling_item
               SET status = 'OPEN', resolved_period_id = NULL, resolved_run_id = NULL,
                   cleared_bank_txn_id = NULL, cleared_gl_entry_id = NULL
             WHERE resolved_period_id = ?""", (period_id,))
        # ... then supersede it (cascade takes its matches, items and events)
        conn.execute("DELETE FROM recon_run WHERE period_id = ?", (period_id,))

        run_id = conn.execute("""
            INSERT INTO recon_run (period_id, run_at, engine_version, params_json,
                                   n_carryforward, n_exact, n_timing, n_tolerance,
                                   n_raised, n_exceptions, proof_diff_cents,
                                   bank_control_cents, gl_control_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (period_id, now, engine_version,
              json.dumps(params if params is not None else current_params(), sort_keys=True),
              len(carried["resolutions"]),
              counts["EXACT"], counts["TIMING"], counts["TOLERANCE"],
              len(new_items), len(exceptions), proof["difference"],
              proof["completeness"]["Bank"]["diff"], proof["completeness"]["GL"]["diff"])
        ).lastrowid

        conn.executemany("""
            INSERT INTO match (run_id, pass, method, bank_txn_id, gl_entry_id,
                               date_delta_days, cents_delta, fuzzy_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [(run_id, m["pass"].upper(), m["method"],
               bank_id[int(m["bank_idx"])], gl_id[int(m["gl_idx"])],
               m["date_delta"], m["cents_delta"], m["score"])
              for m in matches])

        for e in new_items:
            residual = e["Category"] == "RESIDUAL"
            on_bank = e["Side"] == "Bank only"
            source = None if residual else \
                (bank_by_row if on_bank else gl_by_row)[int(e["Source row"])]
            item_id = conn.execute("""
                INSERT INTO reconciling_item (origin_period_id, origin_run_id, side,
                                              bank_txn_id, gl_entry_id,
                                              category, amount_cents)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (period_id, run_id,
                  "RESIDUAL" if residual else ("BANK" if on_bank else "GL"),
                  source if on_bank else None, None if on_bank else source,
                  e["Category"], e["cents"])).lastrowid
            conn.execute("""
                INSERT INTO item_event (item_id, event_at, event_type, period_id, run_id, note)
                VALUES (?, ?, 'CREATED', ?, ?, ?)
            """, (item_id, now, period_id, run_id,
                  f"raised by {e['Category']} — {e['Probable cause']}"))

        for r in carried["resolutions"]:
            on_bank = r["clearing_side"] == "BANK"
            conn.execute("""
                UPDATE reconciling_item
                   SET status = 'CLEARED', resolved_period_id = ?, resolved_run_id = ?,
                       cleared_bank_txn_id = ?, cleared_gl_entry_id = ?
                 WHERE item_id = ? AND status = 'OPEN'
            """, (period_id, run_id,
                  r["clearing_id"] if on_bank else None,
                  None if on_bank else r["clearing_id"],
                  r["item_id"]))
            conn.execute("""
                INSERT INTO item_event (item_id, event_at, event_type, period_id, run_id, note)
                VALUES (?, ?, 'CLEARED', ?, ?, ?)
            """, (r["item_id"], now, period_id, run_id, r["note"]))

    return run_id


def write_off(conn, item_id, period_id, note):
    """An accountant's decision, not the engine's: this item will never clear."""
    with conn:
        changed = conn.execute("""
            UPDATE reconciling_item SET status = 'WRITTEN_OFF', resolved_period_id = ?
             WHERE item_id = ? AND status = 'OPEN'""", (period_id, item_id)).rowcount
        if not changed:
            raise LookupError(f"item {item_id} is not open")
        conn.execute("""
            INSERT INTO item_event (item_id, event_at, event_type, period_id, note)
            VALUES (?, ?, 'WRITTEN_OFF', ?, ?)
        """, (item_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
              period_id, note))


def run_summary(conn, period_id: str):
    """The stored run for a period, or None."""
    return conn.execute("""
        SELECT r.*,
               (SELECT COUNT(*) FROM match m WHERE m.run_id = r.run_id) AS n_matches,
               (SELECT COUNT(*) FROM reconciling_item i WHERE i.origin_run_id = r.run_id
                 AND i.status = 'OPEN') AS n_open
        FROM recon_run r WHERE r.period_id = ? ORDER BY r.run_id DESC LIMIT 1
    """, (period_id,)).fetchone()