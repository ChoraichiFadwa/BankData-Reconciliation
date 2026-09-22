Instead of a simple data reconciliaton tool, introducing a ledger (memory)
The first version reconciled a month. The second version runs the close: reconciling items persist across periods, clear automatically when they show up on the next statement, and age into escalation when they don't. A rollforward control proves nothing was lost between months, and closed periods are locked at the database level.
The ledger (SQL database) is the memory of the close. Every month's exceptions are saved there as open items.
Pass 0 (SQL query) runs first each month and clears prior open items that finally showed up, like the June check that cleared on July 20.
Passes 1–3 (Python, unchanged) reconcile the current month's remaining rows, and whatever's left becomes new open items in the ledger.
Rollforward, aging, and period locking (SQL) also come from the ledger. They're not part of Pass 0, but they're where a lot of the value is.

1) Regression test branch 
