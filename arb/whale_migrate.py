"""L14 whale-tape schema migrations. Split out of arb/whale_tape.py on size
alone (I2, review of a90a08d/fce71ea): that module is already over its ~300
line budget, so a column-migration guard newly needed there moves here
instead of growing it further.

Every DDL in whale_tape.SCHEMA is CREATE TABLE IF NOT EXISTS, so a column
added to an existing tape needs an ALTER: (table, column, type), applied when
PRAGMA table_info says it is missing. The tape is not the ledger — rule 9
forbids UPDATE/DELETE on flat_bets/bet_events, not schema growth here.

`whale` and `settle` both call `open_tape` on process start (Task 4), so the
PRAGMA-check-then-ALTER pair below can race: two processes opening a
pre-migration tape at once can both see the column missing before either
ALTERs. `BEGIN IMMEDIATE` takes sqlite's write lock before the check so the
loser blocks (busy_timeout=30000, set by `open_tape` before this runs) rather
than racing past it, and a "duplicate column name" `OperationalError` from
the loser's own ALTER — it re-checked after acquiring the lock and lost
anyway, because the winner's transaction already committed — is read as
"already done" by another process, not a failure.
"""
from __future__ import annotations

import sqlite3

from arb.logutil import get_logger

log = get_logger("arb.whale", "logs/whale.log")

_MIGRATIONS = (("signals", "holders_ratio", "TEXT"),)    # §7 item 1b, 2026-09-04


def _migrate(con) -> None:
    for table, column, kind in _MIGRATIONS:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if column in cols:
            continue
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            log.info("whale tape: added %s.%s", table, column)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                con.rollback()
                raise
            log.info("whale tape: %s.%s added by another process, racing "
                      "open_tape treated as done", table, column)
        con.commit()
