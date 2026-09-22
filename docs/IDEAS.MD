Instead of a simple data reconciliaton tool, introducing a ledger (memory)
The first version reconciled a month. The second version runs the close: reconciling items persist across periods, clear automatically when they show up on the next statement, and age into escalation when they don't. A rollforward control proves nothing was lost between months, and closed periods are locked at the database level.
The ledger (SQL database) is the memory of the close. Every month's exceptions are saved there as open items.
Pass 0 (SQL query) runs first each month and clears prior open items that finally showed up, like the June check that cleared on July 20.
Passes 1–3 (Python, unchanged) reconcile the current month's remaining rows, and whatever's left becomes new open items in the ledger.
Rollforward, aging, and period locking (SQL) also come from the ledger. They're not part of Pass 0, but they're where a lot of the value is.

1) Regression test 
2) The engine today is a function: two files in, one report out. It has no memory. Everything it learns — that this check is outstanding, that this $48K deposit is in transit — dies when the process exits.

That's fine for one month and wrong for a close, because a reconciliation is not a monthly one-off. It's a continuous control. June's 18 exceptions are not results; they're open items with a future. Most clear in July. One check won't clear for months and needs to age. One deposit may never clear and has to escalate. To do any of that, the engine has to answer questions it currently can't:

What was still open at the end of June?
Did this July bank row clear a June item, or is it new?
How long has this check been outstanding?
Did anything get lost between June's closing items and July's opening items?
What did we run for June, with which parameters, and has anyone changed it since we closed?

Every one of those is a question about state that survives between runs. That's what a database is for. Without it, Pass 0 has no input, aging has nothing to age, and the rollforward has nothing to roll.

Why SQL specifically

The honest framing is that we need persistent, queryable, constrained state. Here's what the alternatives give you:

More CSVs, or a JSON file of open items. This would actually work for storage. What it lacks is constraints and queries. Nothing stops the same item being written twice, or a "cleared" item pointing at a match that doesn't exist. Every question ("open items over 60 days, by cause") becomes a Python loop you write and test yourself. You'd end up reimplementing a weak database.

Excel as the store. Same problems, plus it's hand-editable, which is fatal for something that's supposed to be an audit trail.

Parquet or a dataframe store. Good for analytics, not for this. There's no uniqueness enforcement, no foreign keys, no in-place update of one item's status, and no transactions.

A document store like Mongo. The data here is relational: matches reference two transactions, items reference a match and a period. Joins are the main operation. This is the shape SQL was built for.

Postgres instead of SQLite. Right choice for a real deployment, wrong for a portfolio repo. It needs a server, credentials, and setup, so a reviewer can no longer clone and run. SQLite is a single file, in Python's standard library, and speaks the same SQL. If you later want Postgres, the schema moves with small changes.

What SQL gives us that the alternatives don't, concretely:

Constraints as guarantees. UNIQUE on a row hash makes loading idempotent. Foreign keys make it impossible to record a match to a transaction that isn't there. These hold even if the Python has a bug.
Transactions. A reconciliation run either lands completely or not at all. No half-written months.
Declarative queries. Aging, the rollforward, and Pass 0 are queries, not loops.
Triggers. Period locking is enforced by the database, so a closed month can't be modified by anyone, including a future version of your own code.
It's the thing employers screen for. SQL is on nearly every data engineering and analyst job description. "I used SQLite because the constraints enforce the accounting invariants" is a better answer than "I stored it in a JSON file."
The Phase 1 plan

The engine's behavior does not change in Phase 1. We're building the store and filling it, nothing more.

sql/schema.sql with seven tables: recon_period, bank_txn, gl_entry, recon_run, match, reconciling_item, item_event. Conventions fixed up front: amounts as integer cents (matching the engine), dates as ISO text, foreign keys enabled, and a content hash with UNIQUE on transaction rows.
src/db.py with a handful of functions: create the database from the schema, open a connection with foreign keys on, load a period's three CSVs into recon_period / bank_txn / gl_entry.
Loading is idempotent. Each row gets a hash of its content, so loading the same file twice inserts nothing the second time. This matters because you'll rerun months constantly while developing, and a store that silently doubles its rows would poison every later number.
Tests. Load June twice, assert 127 + 127 rows. Assert a match to a nonexistent transaction is rejected. Assert stored cents equal the CSV values. And critically: the existing 27 tests still pass untouched, because the engine hasn't changed.

The done criterion: python src/db.py --load 2026-06 twice leaves exactly 127 + 127 rows, and pytest is green.

Phase 2 

