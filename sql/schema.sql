-- schema.sql — the reconciliation ledger.
--
-- The engine is stateless: two files in, one report out. This is the memory
-- underneath it. It holds every period's transactions, what each run matched,
-- and every reconciling item as a living object with a status that changes
-- across months.
--
-- Conventions:
--   * money is INTEGER cents, never REAL — same rule as the engine
--   * dates are ISO text ('2026-06-30'); SQLite compares them correctly
--   * periods are 'YYYY-MM' and every table hangs off one, so adding July
--     is data, not a migration
--   * foreign keys are OFF by default in SQLite: db.py turns them on per
--     connection, otherwise the REFERENCES below are documentation, not rules

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- periods
-- One row per month being reconciled. The balances are the STATED figures
-- (bank statement header, GL trial balance) the proof is built on — never
-- derived from the transactions.
CREATE TABLE IF NOT EXISTS recon_period (
    period_id        TEXT    PRIMARY KEY,               -- '2026-06'
    period_start     TEXT    NOT NULL,
    period_end       TEXT    NOT NULL,
    bank_open_cents  INTEGER NOT NULL,
    bank_close_cents INTEGER NOT NULL,
    gl_open_cents    INTEGER NOT NULL,
    gl_close_cents   INTEGER NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'OPEN'
                             CHECK (status IN ('OPEN', 'CLOSED')),
    closed_at        TEXT,
    CHECK (period_id GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    CHECK (period_end >= period_start),
    -- a period is closed exactly when it has a closing timestamp
    CHECK ((status = 'CLOSED') = (closed_at IS NOT NULL))
);

-- ---------------------------------------------------------------- transactions
-- The two source files, one row per line, loaded verbatim.
CREATE TABLE IF NOT EXISTS bank_txn (
    bank_txn_id  INTEGER PRIMARY KEY,
    period_id    TEXT    NOT NULL REFERENCES recon_period(period_id) ON DELETE CASCADE,
    txn_date     TEXT    NOT NULL,
    description  TEXT    NOT NULL,
    reference    TEXT,
    amount_cents INTEGER NOT NULL,
    check_no     TEXT,                                  -- extracted, '' when not a check
    source_file  TEXT    NOT NULL,
    source_row   INTEGER NOT NULL,                      -- line in that file, after the header
    row_hash     TEXT    NOT NULL,
    CHECK (date(txn_date) IS NOT NULL),
    -- the same line of the same file is the same transaction: reloading is a no-op
    UNIQUE (period_id, source_file, source_row)
);

CREATE TABLE IF NOT EXISTS gl_entry (
    gl_entry_id  INTEGER PRIMARY KEY,
    period_id    TEXT    NOT NULL REFERENCES recon_period(period_id) ON DELETE CASCADE,
    txn_date     TEXT    NOT NULL,
    account      TEXT    NOT NULL,
    memo         TEXT    NOT NULL,
    doc_no       TEXT,
    amount_cents INTEGER NOT NULL,
    check_no     TEXT,
    source_file  TEXT    NOT NULL,
    source_row   INTEGER NOT NULL,
    row_hash     TEXT    NOT NULL,
    CHECK (date(txn_date) IS NOT NULL),
    UNIQUE (period_id, source_file, source_row)
);

-- ---------------------------------------------------------------- runs
-- One row per execution. A period can be rerun while it is OPEN; each rerun
-- is recorded rather than overwriting the last, so the history of what was
-- run, with which thresholds, and what it produced is auditable.
CREATE TABLE IF NOT EXISTS recon_run (
    run_id             INTEGER PRIMARY KEY,
    period_id          TEXT    NOT NULL REFERENCES recon_period(period_id) ON DELETE CASCADE,
    run_at             TEXT    NOT NULL,
    engine_version     TEXT    NOT NULL,
    params_json        TEXT    NOT NULL,                -- thresholds used by this run
    n_carryforward     INTEGER NOT NULL DEFAULT 0,      -- pass 0, arrives in phase 4
    n_exact            INTEGER NOT NULL DEFAULT 0,
    n_timing           INTEGER NOT NULL DEFAULT 0,
    n_tolerance        INTEGER NOT NULL DEFAULT 0,
    n_exceptions       INTEGER NOT NULL DEFAULT 0,
    proof_diff_cents   INTEGER,
    bank_control_cents INTEGER,                         -- completeness controls
    gl_control_cents   INTEGER
);

-- ---------------------------------------------------------------- matches
-- Every pair the engine made, with the pass and method that made it.
-- The UNIQUE constraints are the one-to-one rule, enforced by the database:
-- within a run, no transaction can be consumed twice. That is what forces a
-- duplicate GL posting out into the exception report.
CREATE TABLE IF NOT EXISTS match (
    match_id        INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES recon_run(run_id) ON DELETE CASCADE,
    pass            TEXT    NOT NULL
                            CHECK (pass IN ('CARRYFWD', 'EXACT', 'TIMING', 'TOLERANCE')),
    method          TEXT    NOT NULL,                   -- 'check no.', 'date window', 'fuzzy', ...
    bank_txn_id     INTEGER NOT NULL REFERENCES bank_txn(bank_txn_id) ON DELETE CASCADE,
    gl_entry_id     INTEGER NOT NULL REFERENCES gl_entry(gl_entry_id) ON DELETE CASCADE,
    date_delta_days INTEGER NOT NULL,
    cents_delta     INTEGER NOT NULL,
    fuzzy_score     REAL,
    UNIQUE (run_id, bank_txn_id),
    UNIQUE (run_id, gl_entry_id)
);

-- ---------------------------------------------------------------- reconciling items
-- The heart of the ledger: an exception as a living object. It is raised in
-- one period and resolved in a later one, or it ages.
CREATE TABLE IF NOT EXISTS reconciling_item (
    item_id            INTEGER PRIMARY KEY,
    origin_period_id   TEXT    NOT NULL REFERENCES recon_period(period_id) ON DELETE CASCADE,
    origin_run_id      INTEGER NOT NULL REFERENCES recon_run(run_id) ON DELETE CASCADE,
    side               TEXT    NOT NULL CHECK (side IN ('BANK', 'GL')),
    bank_txn_id        INTEGER REFERENCES bank_txn(bank_txn_id) ON DELETE CASCADE,
    gl_entry_id        INTEGER REFERENCES gl_entry(gl_entry_id) ON DELETE CASCADE,
    category           TEXT    NOT NULL,                -- 'DIT', 'OS_CHECK', ... (engine codes)
    amount_cents       INTEGER NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'OPEN'
                               CHECK (status IN ('OPEN', 'CLEARED', 'WRITTEN_OFF')),
    escalated          INTEGER NOT NULL DEFAULT 0 CHECK (escalated IN (0, 1)),
    resolved_period_id TEXT    REFERENCES recon_period(period_id),
    resolved_match_id  INTEGER REFERENCES match(match_id) ON DELETE SET NULL,
    -- an item belongs to exactly one source row, on the side it says it does
    CHECK ((bank_txn_id IS NULL) <> (gl_entry_id IS NULL)),
    CHECK ((side = 'BANK') = (bank_txn_id IS NOT NULL)),
    -- resolved exactly when a resolving period is recorded: the rollforward
    -- (opening + new − cleared − written off = closing) cannot silently drift
    CHECK ((status = 'OPEN') = (resolved_period_id IS NULL))
);

-- one item per source row per run (partial: the other side's column is NULL)
CREATE UNIQUE INDEX IF NOT EXISTS ux_item_bank
    ON reconciling_item (origin_run_id, bank_txn_id) WHERE bank_txn_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_item_gl
    ON reconciling_item (origin_run_id, gl_entry_id) WHERE gl_entry_id IS NOT NULL;

-- ---------------------------------------------------------------- item history
-- Append-only. Nothing here is ever edited: a change of mind is a new event.
CREATE TABLE IF NOT EXISTS item_event (
    event_id   INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES reconciling_item(item_id) ON DELETE CASCADE,
    event_at   TEXT    NOT NULL,
    event_type TEXT    NOT NULL CHECK (event_type IN
                       ('CREATED', 'CLEARED', 'WRITTEN_OFF', 'ESCALATED', 'NOTE')),
    period_id  TEXT    NOT NULL REFERENCES recon_period(period_id) ON DELETE CASCADE,
    run_id     INTEGER REFERENCES recon_run(run_id) ON DELETE SET NULL,
    note       TEXT
);

-- ---------------------------------------------------------------- indexes
-- Matching reads transactions by period + amount (and by check number);
-- pass 0 and the aging report read items by status.
CREATE INDEX IF NOT EXISTS ix_bank_amount ON bank_txn (period_id, amount_cents);
CREATE INDEX IF NOT EXISTS ix_bank_check  ON bank_txn (period_id, check_no);
CREATE INDEX IF NOT EXISTS ix_gl_amount   ON gl_entry (period_id, amount_cents);
CREATE INDEX IF NOT EXISTS ix_gl_check    ON gl_entry (period_id, check_no);
CREATE INDEX IF NOT EXISTS ix_item_open   ON reconciling_item (status, origin_period_id);
CREATE INDEX IF NOT EXISTS ix_event_item  ON item_event (item_id, event_at);