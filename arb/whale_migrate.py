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

_MIGRATIONS = (("signals", "holders_ratio", "TEXT"),      # §7 item 1b, 2026-09-04
                ("markets", "volume_24h", "TEXT"))        # non-sports roots, 2026-09-04

# The two `markets` triggers arb/whale_mat.py's design §5 item 5 needs (that
# module's docstring is the design; the DDL lives here because replacing an
# OLD tape's copies is a MIGRATION, and a migration's before and after belong
# in one file). `whale_mat.MAT_SCHEMA` appends these, so `open_tape` creates
# them IF NOT EXISTS — never a write on a tape that already has them.
#
# C1 (fix pass 3): df97ad3 fixed the BODY by putting `DROP TRIGGER IF EXISTS`
# into MAT_SCHEMA, which `open_tape` runs on EVERY open. That made every child
# start-up a WRITER — reproduced `database is locked` after the busy_timeout
# against the backfill child's long `persist_markets` transaction — and, since
# `executescript` autocommits each DDL, left the tape trigger-less for a window
# on every open, permanently so if the script aborted mid-way. The marker below
# is what a NEW body carries and the pre-df97ad3 `INSERT OR IGNORE` one does
# not, so `_migrate_triggers` can replace exactly the old ones, exactly once.
# Change this string whenever `_ENQUEUE` changes — it is a one-shot STAMP a
# trigger body carries, not a version number to bump for its own sake.
MAT_TRIGGER_MARK = "NOT EXISTS (SELECT 1 FROM mat_dirty"
_ENQUEUE = f"""
    BEGIN
        INSERT INTO mat_dirty (proxy_wallet) SELECT DISTINCT p.proxy_wallet FROM prints p
         WHERE p.condition_id = NEW.condition_id
           AND {MAT_TRIGGER_MARK} d WHERE d.proxy_wallet = p.proxy_wallet);
    END;"""
MAT_TRIGGERS = {
    "trg_markets_mat_update":
        "CREATE TRIGGER IF NOT EXISTS trg_markets_mat_update AFTER UPDATE ON markets"
        " WHEN NEW.sport IS NOT OLD.sport OR NEW.market_type IS NOT OLD.market_type"
        " OR NEW.start_utc IS NOT OLD.start_utc" + _ENQUEUE,
    "trg_markets_mat_insert":
        "CREATE TRIGGER IF NOT EXISTS trg_markets_mat_insert AFTER INSERT ON markets"
        " WHEN NEW.sport IS NOT NULL" + _ENQUEUE,
}


def _migrate_triggers(con) -> None:
    """Replace a pre-df97ad3 `markets` trigger pair, once, atomically.

    The stale read outside the lock costs nothing if another process wins the
    race: DROP+CREATE of the same body is idempotent, and it happens inside
    this transaction, so the loser rewrites what the winner wrote rather than
    needing the ALTER path's `duplicate column name` handling. SQLite DDL is
    transactional, so a failure anywhere leaves the OLD pair in place — the
    tape is never trigger-less."""
    stale = [name for name, row in
             ((n, con.execute("SELECT sql FROM sqlite_master WHERE type='trigger'"
                              " AND name=?", (n,)).fetchone()) for n in MAT_TRIGGERS)
             if row and MAT_TRIGGER_MARK not in (row[0] or "")]
    if not stale:
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        for name in stale:
            con.execute(f"DROP TRIGGER IF EXISTS {name}")
            con.execute(MAT_TRIGGERS[name])
    except Exception:
        con.rollback()
        raise
    con.commit()
    log.info("whale tape: replaced %d pre-df97ad3 markets trigger(s)", len(stale))


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
    _migrate_triggers(con)


# Fix pass 4 (controller, live tape census 2026-09-05): the ~52 `markets`
# rows the pre-fix `_sport_of` (arb/whale_map.py) inexact-prefix-without-a-
# date bug mislabelled — `sport`/`market_type`/`start_utc` all land non-NULL
# for one of these (an inexact false positive stores exactly like a real
# sports row), so REVISIT_SQL never reconsiders them and their prints keep
# contaminating the wrong sport's wallet buckets, permanently. Not schema, so
# it does not belong in `_MIGRATIONS`/`_migrate` above (no `cfg` reaches
# `open_tape`, and `slug_sport_prefixes` is what decides which rows are
# affected) — this one is called from `whale_backfill.map_markets`, which
# already has both. `markets` is a mutable CACHE table, not the append-only
# ledger (rule 9 governs `flat_bets`/`bet_events`, not this), so a targeted
# UPDATE is allowed, and it trips the `markets` UPDATE trigger (arb/
# whale_mat.py design §5 item 5), so every wallet holding one of these
# positions re-materialises on its own. The marker carries the census DATE
# on purpose (fix pass 5, review of 333e264): a future `_sport_of` reading
# needs its own repair pass over a different `doomed` set, and reusing this
# marker would skip it as already-done — give that pass a NEW dated marker.
SLUG_PREFIX_FIX_MARK = "slug_prefix_fix_2026-09-05"


def repair_slug_prefix_mismap(cfg, con) -> int:
    """One-shot, idempotent: reset every `markets` row whose stored `sport`
    is exactly what the PRE-fix `_sport_of` would have matched from its own
    slug (`require_date_for_inexact=False`) but the FIXED reading no longer
    matches — i.e. exactly the rows fix pass 4 now roots differently. Reuses
    `_sport_of` for both readings so the matching rule lives in the one
    place, never re-typed here for comparison.

    Marker-gated like `_migrate_triggers` above, and unlike `unroot_mechanical`
    (arb/whale_ns_map.py, self-terminating by its own SELECT): a reset row's
    `sport` becomes NULL, which no longer equals the PRE-fix reading either,
    so the affected set is not naturally self-excluding the way a still-
    non-sports-rooted row is — without the marker this would rescan every
    non-NULL-sport row in `markets` every cycle forever for a bug fixed once.

    Check, UPDATE and marker share ONE `BEGIN IMMEDIATE` (fix pass 5, review
    of 333e264), like `_migrate_triggers` above: unlike that DROP+CREATE pair,
    a reset here is not naturally idempotent under a race — two processes
    both reading the marker as absent could both compute `doomed` and both
    log/UPDATE it. The lock forces a loser to see the marker the winner just
    set."""
    from arb.whale_map import _sport_of
    from arb.whale_tape import _meta, _set_meta
    con.execute("BEGIN IMMEDIATE")
    try:
        if _meta(con, SLUG_PREFIX_FIX_MARK) is not None:
            con.rollback()
            return 0
        prefixes = cfg["whale"]["slug_sport_prefixes"]
        doomed = [r["condition_id"] for r in con.execute(
            "SELECT condition_id, slug, sport FROM markets"
            " WHERE sport IS NOT NULL AND slug IS NOT NULL")
            if _sport_of(r["slug"], prefixes, require_date_for_inexact=False) == r["sport"]
            and _sport_of(r["slug"], prefixes) is None]
        if doomed:
            con.executemany(
                "UPDATE markets SET sport=NULL, market_type=NULL, start_utc=NULL"
                " WHERE condition_id=?", [(c,) for c in doomed])
        _set_meta(con, SLUG_PREFIX_FIX_MARK, str(len(doomed)))  # commits the txn
    except Exception:
        con.rollback()
        raise
    if doomed:
        log.info("whale tape: reset %d slug-prefix-mismapped market row(s)", len(doomed))
    return len(doomed)
