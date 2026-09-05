"""L14: per-wallet materialization of positions + hygiene (matpos, 2026-09-05).
WRITE side — DDL (`MAT_SCHEMA`), the dirty queue, `materialize`/`_wallet`.
READ side (`load_positions`, `load_hygiene`, `freshness`/`warn_if_stale`,
`cache_partial`) moved to `arb/whale_mat_read.py` on size (2026-09-05),
re-exported below so every `whale_mat.load_positions` etc. caller is unaffected.

`whale_state.score_state` used to rebuild EVERY wallet's positions and hygiene
flags from EVERY print on every 60 s cycle. Profiled live 2026-09-05 on the
production tape (763,786 prints): 29.4 s, 57,290 positions — against a 60 s
cadence and a 120 s child cap, with the tape growing ~700k rows/day. Cost is
O(prints); the fix is to recompute only what changed.

What is cached: `positions_mat` mirrors `whale_score.Position` field for
field; `hygiene_mat` mirrors `whale_hygiene.Hygiene` (one row per flag — a
NULL `condition_id` is a WALLET-level flag, non-null is that wallet's flag in
one market). Both are DERIVED — `prints` remains the record (rule 7) and this
cache can be dropped and rebuilt from it at any time, so DELETE+INSERT here is
not a rule-9 concern (that rule binds `flat_bets`/`bet_events` in the ledger).
`positions_from_prints`/`hygiene_flags` stay the single source of the FIFO and
hygiene logic; this module calls them once per dirty wallet.

Why per-WALLET, never per-print: FIFO SELL matching needs a wallet's whole
history in ts order, so a backfill inserting OLD prints changes positions it
already had — recomputing the wallet whole (what `_wallet` does) is the only
correct granularity.

The dirty set (why this is O(changed), not O(prints)): `prints.id` is a
monotonic INTEGER PRIMARY KEY on an append-only table, so every row written
since the last pass is the rowid range `(watermark, MAX(id)]` — a scan of the
NEW rows, never the whole tape. Wallets in that range are queued in
`mat_dirty` and the watermark advances in the SAME transaction, so a second
writer (the backfill child) either sees the rows itself or finds them already
queued. `mat_dirty` drains oldest-first, `whale.mat.max_wallets_per_cycle` a
call, so a first pass over an existing tape spreads over cycles.

Markets change too (design §5 item 5): a market gaining a `sport` turns
scoreless prints into positions, and a moved `start_utc` changes `pregame`.
Handled by the two `markets` triggers in MAT_SCHEMA — any writer, any module,
rather than a hook every market writer would remember — whose WHEN clause
keeps `persist_markets`' 60 s universe rewrite from dirtying everything (only
a market whose sport/market_type/start_utc actually MOVES dirties wallets).

`MAT_VERSION` is the escape hatch: bump it alongside any FIFO/hygiene logic
change and every wallet re-materializes on the next pass, no operator action.
"""
from __future__ import annotations

import time
from collections.abc import Mapping

from arb import whale_score
from arb.whale_hygiene import hygiene_flags
from arb.whale_mat_read import (POSITION_COLS, cache_partial, freshness,  # noqa: F401
                                load_hygiene, load_positions, warn_if_stale)
from arb.whale_tape import _meta

# Bump on ANY change to positions_from_prints / hygiene_flags semantics.
MARKETS_CHUNK = 500   # SQLite's default parameter limit is 999; PK IN(...) seeks per wallet

MAT_VERSION = 1

# The cache's own DDL, appended to whale_tape.SCHEMA by `open_tape`. It lives
# here, with the only module that reads or writes these four objects, because
# whale_tape.py is already over its ~300-line budget (the same reason
# arb/whale_migrate.py was split out of it). Every statement is
# CREATE ... IF NOT EXISTS, so it is self-migrating on an existing tape —
# open_tape runs it on every open — exactly like the rest of SCHEMA.
MAT_SCHEMA = """
-- matpos (2026-09-05): the DERIVED positions/hygiene cache — arb/whale_mat.py
-- owns every read and write of these four objects, and its docstring is the
-- design. whale_tape.SCHEMA's `prints` stays the record (rule 7); this cache
-- may be dropped and rebuilt from it (`arb whale-mat --rebuild`), which is
-- why DELETE+INSERT here is not rule 9 (that binds flat_bets/bet_events).
-- Columns/INSERT: arb/whale_mat_read.py POSITION_COLS, mirroring
-- whale_score.Position field for field (kept in sync by hand, like `prints`).
CREATE TABLE IF NOT EXISTS positions_mat (
    proxy_wallet TEXT, condition_id TEXT, outcome_index INTEGER, avg_price TEXT,
    size TEXT, notional_usd TEXT, entry_ts INTEGER, last_ts INTEGER,
    pregame INTEGER, sport TEXT, market_type TEXT, root TEXT, source TEXT,
    sold_size TEXT, sell_value TEXT);
-- Serves BOTH the per-wallet DELETE materialize does and the full-table read
-- score_state does in whale_score.positions_from_prints's own output order
-- (arb/whale_mat_read.py POSITION_ORDER), so one index covers the module.
CREATE INDEX IF NOT EXISTS idx_positions_mat
    ON positions_mat(proxy_wallet, condition_id, outcome_index, entry_ts);
-- One row per flag. NULL condition_id = a WALLET-level flag (mm/wash);
-- non-null = that wallet's flag in that one market (both_sides) — the two
-- scopes whale_hygiene.Hygiene carries.
CREATE TABLE IF NOT EXISTS hygiene_mat (
    proxy_wallet TEXT, condition_id TEXT, flag TEXT);
CREATE INDEX IF NOT EXISTS idx_hygiene_mat ON hygiene_mat(proxy_wallet);
-- The work queue. rowid order IS discovery order, so draining by rowid is
-- oldest-dirty-first with no timestamp column to keep.
CREATE TABLE IF NOT EXISTS mat_dirty (proxy_wallet TEXT PRIMARY KEY);
-- Design §5 item 5, in SQL rather than a hook every `markets` writer would
-- have to remember: a market gaining a `sport` turns scoreless prints into
-- positions, and a moved `start_utc` changes `pregame` for cached positions.
-- The WHEN clause (`IS NOT`, NULL-safe) is what keeps `persist_markets`'
-- 60 s universe rewrite cheap: MEASURED 2026-09-05, a no-op re-check of 1,000
-- markets costs 0.00 s; a REAL re-map of 1,000 costs 46 s (uncovered DISTINCT
-- scan below) — lands in the whale-backfill child (900 s cap, 600 s cadence),
-- never the 60 s lane, and only when a mapping actually changes. ponytail: a
-- covering prints(condition_id, proxy_wallet) index would cut that, deliberately
-- not added — a 5th index on the hottest write table for a rare path.
DROP TRIGGER IF EXISTS trg_markets_mat_update;  -- NOT EXISTS below: an upsert overrides OR IGNORE (2026-09-05)
CREATE TRIGGER trg_markets_mat_update AFTER UPDATE ON markets
    WHEN NEW.sport IS NOT OLD.sport OR NEW.market_type IS NOT OLD.market_type OR NEW.start_utc IS NOT OLD.start_utc
    BEGIN
        INSERT INTO mat_dirty (proxy_wallet) SELECT DISTINCT p.proxy_wallet FROM prints p
         WHERE p.condition_id = NEW.condition_id
           AND NOT EXISTS (SELECT 1 FROM mat_dirty d WHERE d.proxy_wallet = p.proxy_wallet);
    END;
DROP TRIGGER IF EXISTS trg_markets_mat_insert;
CREATE TRIGGER trg_markets_mat_insert AFTER INSERT ON markets WHEN NEW.sport IS NOT NULL
    BEGIN
        INSERT INTO mat_dirty (proxy_wallet) SELECT DISTINCT p.proxy_wallet FROM prints p
         WHERE p.condition_id = NEW.condition_id
           AND NOT EXISTS (SELECT 1 FROM mat_dirty d WHERE d.proxy_wallet = p.proxy_wallet);
    END;
"""

# Exactly what positions_from_prints, hygiene_flags and the lane's `_from_prints`
# read off a print. `SELECT *` also pulled `raw_json` — the venue's whole payload
# for every print ever collected.
PRINT_COLS = ("ts, proxy_wallet, condition_id, outcome_index, side, price, size,"
              " notional_usd, source, asset, event_slug")
_PRINT_FIELDS = tuple(c.strip() for c in PRINT_COLS.split(","))
_PRINT_IDX = {name: i for i, name in enumerate(_PRINT_FIELDS)}
# positions_from_prints groups by (proxy_wallet, condition_id, outcome_index)
# and needs each group's own rows in ascending ts. With `WHERE proxy_wallet=?`
# this is a straight seek on idx_prints_score (arb/whale_tape.py SCHEMA).
PRINT_ORDER = "ORDER BY proxy_wallet, condition_id, outcome_index, ts"

# Everything positions_from_prints (sport, market_type, start_utc), the lane's
# `_tape_market`/`_event_key` for an aged-out held market (line, event_key,
# tokens_json, slug) and the dashboard read off a `markets` row. `SELECT *`
# also carried first_seen/last_seen/volume_24h, which nothing reads back
# through `state.markets`. Lives here because `materialize` and
# `whale_state.score_state` must read the SAME columns — one constant, so a
# column a position needs cannot go missing from one of the two.
MARKET_COLS = ("condition_id, sport, market_type, line, start_utc, event_key,"
               " tokens_json, slug")

# `POSITION_COLS` (`positions_mat` IS the Position dataclass, in its field
# order) lives in whale_mat_read.py — imported above — since it also shapes
# that module's `load_positions` SELECT; `_wallet` below reuses the same
# constant for its INSERT so the two can never drift apart on column order.


class _PrintRow(Mapping):
    """One `prints` row, `PRINT_COLS`-shaped, over a bare tuple — no `dict`
    built (or hashed) per row. `Mapping` gives `get`/`in`/`keys`/`items`/
    `values`/`__eq__` for free off `__getitem__`+`__iter__`+`__len__`, so
    `row["field"]`, `row.get("source", "tape")`, `"x" in row` and `set(row)`
    (the exact surface positions_from_prints and hygiene_flags use) all work."""
    __slots__ = ("_v",)

    def __init__(self, values):
        self._v = values

    def __getitem__(self, key):
        return self._v[_PRINT_IDX[key]]

    def __iter__(self):
        return iter(_PRINT_FIELDS)

    def __len__(self):
        return len(_PRINT_FIELDS)


def _put(con, key, value) -> None:
    """`whale_tape._set_meta` without its commit: the watermark must land in
    the SAME transaction as the dirty rows it accounts for."""
    con.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))


def mark_all_dirty(con) -> int:
    """Queue every wallet the tape has a print for — the forced full rebuild
    (`arb whale-mat --rebuild`) and what a `MAT_VERSION` bump does by itself.
    A covering scan of idx_prints_wallet, not of the table."""
    con.execute("INSERT OR IGNORE INTO mat_dirty (proxy_wallet) "
                "SELECT DISTINCT proxy_wallet FROM prints")
    _put(con, "mat_watermark", con.execute("SELECT MAX(id) FROM prints").fetchone()[0] or 0)
    _put(con, "mat_version", MAT_VERSION)
    # ... and that until the queue drains the POPULATION is not the tape's
    # (`cache_partial` below, and I1 in the review of 783301f).
    _put(con, "mat_rebuilding", 1)
    con.commit()
    return con.execute("SELECT COUNT(*) c FROM mat_dirty").fetchone()[0]


def _enqueue(con) -> None:
    """Wallets with a print written since the last pass. The watermark and the
    rows it covers commit together, so a concurrent writer either queues them
    itself or is already covered — never neither."""
    if str(_meta(con, "mat_version") or "") != str(MAT_VERSION):
        mark_all_dirty(con)
        return
    lo = int(_meta(con, "mat_watermark") or 0)
    hi = con.execute("SELECT MAX(id) FROM prints").fetchone()[0] or 0
    if hi <= lo:
        return
    con.execute("INSERT OR IGNORE INTO mat_dirty (proxy_wallet) SELECT DISTINCT "
                "proxy_wallet FROM prints WHERE id > ? AND id <= ?", (lo, hi))
    _put(con, "mat_watermark", hi)
    con.commit()


def _markets_for(con, cids) -> dict:
    """The `markets` rows for exactly the markets this wallet printed in —
    a primary-key seek each, instead of the whole table. Chunked because a
    heavy wallet can print in more markets than SQLite binds parameters."""
    cids, out = list(cids), {}
    for i in range(0, len(cids), MARKETS_CHUNK):
        chunk = cids[i:i + MARKETS_CHUNK]
        for r in con.execute(f"SELECT {MARKET_COLS} FROM markets WHERE condition_id"
                             f" IN ({','.join('?' * len(chunk))})", chunk):
            out[r["condition_id"]] = dict(r)
    return out


def _wallet(con, cfg, wallet) -> None:
    """One wallet, recomputed WHOLE from its own prints and written in one
    IMMEDIATE transaction: the cache is never half a wallet.

    The `markets` read is INSIDE that transaction, and that is load-bearing
    (C1, review of 783301f; design §5 amendment 2026-09-05b). Off a per-PASS
    snapshot, a sport/market_type/start_utc change another writer committed
    mid-pass was applied STALE and then dequeued — the triggers' `INSERT OR
    IGNORE` is a no-op for a wallet already queued — so the cache diverged for
    good on any wallet quiet enough not to be dirtied again. Taking the write
    lock first leaves only two orderings, since SQLite serializes writers: the
    change committed before this read (we see it), or after our COMMIT (its
    trigger re-queues this wallet).
    """
    cur = con.cursor()
    cur.row_factory = None                      # bare tuples for _PrintRow
    prints = [_PrintRow(r) for r in cur.execute(
        f"SELECT {PRINT_COLS} FROM prints WHERE proxy_wallet = ? {PRINT_ORDER}",
        (wallet,))]
    # A print written after this read is above `mat_watermark`, so the next
    # `_enqueue` re-queues the wallet — it needs no lock. The markets read does.
    if not con.in_transaction:                  # ponytail: no caller nests one
        con.execute("BEGIN IMMEDIATE")
    markets = _markets_for(con, {p["condition_id"] for p in prints})
    positions = whale_score.positions_from_prints(prints, markets, cfg)
    hyg = hygiene_flags(prints, positions, cfg)
    con.execute("DELETE FROM positions_mat WHERE proxy_wallet = ?", (wallet,))
    con.execute("DELETE FROM hygiene_mat WHERE proxy_wallet = ?", (wallet,))
    # str(Decimal) round-trips exactly, which is what "identical, bit for bit"
    # needs: these are a SERIALIZATION of what positions_from_prints already
    # computed under rule 5, not a second money computation to re-quantize.
    con.executemany(
        f"INSERT INTO positions_mat ({POSITION_COLS}) VALUES ({','.join('?' * 15)})",
        [(p.wallet, p.condition_id, p.outcome_index, str(p.avg_price), str(p.size),
          str(p.notional_usd), p.entry_ts, p.last_ts, int(p.pregame), p.sport,
          p.market_type, p.root, p.source, str(p.sold_size), str(p.sell_value))
         for p in positions])
    con.executemany(
        "INSERT INTO hygiene_mat (proxy_wallet, condition_id, flag) VALUES (?,?,?)",
        [(wallet, None, f) for f in hyg.wallets.get(wallet, ())]
        + [(w, cid, f) for (w, cid), flags in hyg.markets.items() for f in flags])
    con.execute("DELETE FROM mat_dirty WHERE proxy_wallet = ?", (wallet,))
    con.commit()


def materialize(con, cfg, now, cap=None) -> dict:
    """Bring the cache up to date, bounded. Called at the start of every whale
    cycle; `cap=0` means NO BOUND — the forced rebuild's own path, never a
    lane cycle's, which is why `whale.mat.max_wallets_per_cycle >= 1` is a
    loader invariant (M4). Otherwise that many wallets a call, oldest-dirty
    first.

    Returns the ops numbers the cycle dict and the daily ops line print: how
    big the cache is, how far behind it still is, and what this pass cost.
    """
    t0 = time.monotonic()
    res = {"wallets": 0, "dirty": 0, "rows": 0, "secs": 0.0}
    _enqueue(con)
    if cap is None:
        cap = int(cfg["whale"]["mat"]["max_wallets_per_cycle"])
    todo = [r[0] for r in con.execute(
        "SELECT proxy_wallet FROM mat_dirty ORDER BY rowid" + (" LIMIT ?" if cap else ""),
        (cap,) if cap else ())]
    for wallet in todo:
        _wallet(con, cfg, wallet)
        res["wallets"] += 1
    res["dirty"] = con.execute("SELECT COUNT(*) c FROM mat_dirty").fetchone()[0]
    res["rows"] = con.execute("SELECT COUNT(*) c FROM positions_mat").fetchone()[0]
    # What a read-only reader needs to judge this cache without a write handle
    # (I2): when a pass last ran, and when the queue was last actually EMPTY.
    _put(con, "mat_at", now.isoformat())
    if not res["dirty"]:
        _put(con, "mat_clean_at", now.isoformat())
        _put(con, "mat_rebuilding", 0)
    con.commit()
    res["secs"] = round(time.monotonic() - t0, 3)
    return res
