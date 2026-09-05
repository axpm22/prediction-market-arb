"""L14: per-wallet materialization of positions + hygiene (matpos, 2026-09-05).

`whale_state.score_state` rebuilt EVERY wallet's positions and hygiene flags
from EVERY print on every 60 s cycle. Profiled live 2026-09-05 on the
production tape (763,786 prints): 29.4 s, 57,290 positions — against a 60 s
cadence and a 120 s child cap, with the tape growing ~700k rows/day while
2,700 wallet histories backfill. Read-path trims (2026-09-04 column trim,
2026-09-05 `_PrintRow`) bought constants; the cost is O(prints) and the fix is
to recompute only what changed.

What is cached, and what is not:
  `positions_mat` mirrors `whale_score.Position` field for field, and
  `hygiene_mat` mirrors `whale_hygiene.Hygiene` (one row per flag; a NULL
  `condition_id` is a WALLET-level flag, a non-null one is that wallet's flag
  in that one market). Both are DERIVED — `prints` remains the record (rule 7)
  and this cache can be dropped and rebuilt from it at any time, which is why
  DELETE+INSERT here is not a rule-9 concern: rule 9 binds `flat_bets`/
  `bet_events` in the LEDGER, and this is the tape.

`positions_from_prints` and `hygiene_flags` stay the single source of the FIFO
and hygiene logic. This module CALLS them, once per dirty wallet, over that
wallet's own prints — never a second implementation.

Why per-WALLET and never per-print: FIFO SELL matching and the position window
need a wallet's whole history in ts order, so a backfill inserting OLD prints
for a wallet changes positions the wallet already had. Recomputing that wallet
whole is the only correct granularity, and it is what `_wallet` does.

The dirty set (the reason this is O(changed), not O(prints)):
  `prints.id` is a monotonic INTEGER PRIMARY KEY on an append-only table, so
  every row written since the last pass is the rowid range `(watermark, MAX(id)]`
  — a range scan of the NEW rows, never the whole tape. The wallets in that
  range are queued in `mat_dirty` and the watermark advances in the SAME
  transaction, so a second process (the backfill child writes prints too)
  either sees the rows itself or finds them already queued. `mat_dirty` is
  drained oldest-first (rowid order) at `whale.mat.max_wallets_per_cycle` a
  call, so the first pass over an existing tape spreads over cycles instead of
  stalling the lane.

Markets change too (design §5 item 5): a market that gains a `sport` turns
prints that scored nothing into positions, and a `start_utc` filled or moved
later changes `pregame`. That is handled in SQL, by the two `markets` triggers
in MAT_SCHEMA below — any writer, any module — rather than by a hook every
market writer would have to remember. The trigger's WHEN clause is what keeps
`persist_markets` rewriting the universe every 60 s from dirtying everything:
only a market whose sport/market_type/start_utc actually MOVES dirties its
wallets.

`MAT_VERSION` is the escape hatch: bump it in the same commit as any change to
the FIFO or hygiene logic and every wallet re-materializes on the next pass,
with no operator action.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from decimal import Decimal

from arb import whale_score
from arb.whale_hygiene import Hygiene, hygiene_flags
from arb.whale_score import Position
from arb.whale_tape import _meta

# Bump on ANY change to positions_from_prints / hygiene_flags semantics.
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
-- design. whale_tape.SCHEMA's `prints` stays the record (rule 7); this is a
-- cache and may be
-- dropped and rebuilt from it (`arb whale-mat --rebuild`), which is why
-- DELETE+INSERT here is not rule 9 (that binds flat_bets/bet_events in the
-- LEDGER, unreachable from this file).
-- positions_mat columns/INSERT: arb/whale_mat.py POSITION_COLS, which mirrors
-- whale_score.Position field for field (kept in sync by hand, like `prints`).
CREATE TABLE IF NOT EXISTS positions_mat (
    proxy_wallet TEXT, condition_id TEXT, outcome_index INTEGER, avg_price TEXT,
    size TEXT, notional_usd TEXT, entry_ts INTEGER, last_ts INTEGER,
    pregame INTEGER, sport TEXT, market_type TEXT, root TEXT, source TEXT,
    sold_size TEXT, sell_value TEXT);
-- Serves BOTH the per-wallet DELETE materialize does and the full-table read
-- score_state does in whale_score.positions_from_prints's own output order
-- (arb/whale_mat.py POSITION_ORDER), so one index covers the whole module.
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
-- Design §5 item 5, in SQL rather than in a hook every `markets` writer would
-- have to remember: a market that gains a `sport` turns prints that scored
-- NOTHING into positions, and a `start_utc` filled or moved later changes
-- `pregame` for positions already cached. The WHEN clause is what keeps this
-- cheap — `persist_markets` rewrites every universe market every 60 s cycle,
-- and only a market whose sport/market_type/start_utc actually MOVES (`IS NOT`,
-- so NULL-safe) dirties the wallets that printed in it. MEASURED 2026-09-05 on
-- a synthetic 800k-print / 2,000-market tape: that no-op 60 s rewrite of 1,000
-- markets fires nothing and costs 0.00 s.
-- ponytail: the DISTINCT below runs on idx_prints_market(condition_id, ts),
-- which does NOT cover proxy_wallet, so each matching print costs a rowid
-- lookup — 46 s for a REAL re-map of 1,000 markets on that same synthetic tape
-- (400 prints/market; the production tape averages ~55, so ~7x cheaper). That
-- lands in the whale-backfill child (900 s cap, 600 s cadence, ~180 s margin),
-- never in the 60 s lane, and only when a mapping actually changes. If a
-- legacy re-map ever makes it bind, the upgrade is a covering
-- prints(condition_id, proxy_wallet) index — deliberately NOT added now: a
-- fifth index on the hottest write table (~1M inserts/day) to speed up a rare
-- path is the wrong trade.
CREATE TRIGGER IF NOT EXISTS trg_markets_mat_update AFTER UPDATE ON markets
    WHEN NEW.sport IS NOT OLD.sport OR NEW.market_type IS NOT OLD.market_type
         OR NEW.start_utc IS NOT OLD.start_utc
    BEGIN
        INSERT OR IGNORE INTO mat_dirty (proxy_wallet)
            SELECT DISTINCT proxy_wallet FROM prints
             WHERE condition_id = NEW.condition_id;
    END;
CREATE TRIGGER IF NOT EXISTS trg_markets_mat_insert AFTER INSERT ON markets
    WHEN NEW.sport IS NOT NULL
    BEGIN
        INSERT OR IGNORE INTO mat_dirty (proxy_wallet)
            SELECT DISTINCT proxy_wallet FROM prints
             WHERE condition_id = NEW.condition_id;
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

# `positions_mat` IS the Position dataclass, in its field order.
POSITION_COLS = ("proxy_wallet, condition_id, outcome_index, avg_price, size,"
                 " notional_usd, entry_ts, last_ts, pregame, sport, market_type,"
                 " root, source, sold_size, sell_value")
# The order positions_from_prints itself produced: SQLite hands prints back in
# PRINT_ORDER, so groups were visited in (wallet, cid, outcome_index) order and
# a group's windows appended in ts order. Same collation (BINARY / UTF-8 bytes
# == code points), so this reproduces that list exactly — order matters because
# the equivalence tests compare `state.positions` as a LIST.
POSITION_ORDER = "ORDER BY proxy_wallet, condition_id, outcome_index, entry_ts"


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


def _wallet(con, cfg, wallet, markets) -> None:
    """One wallet, recomputed WHOLE from its own prints and written in one
    transaction: the cache is never half a wallet."""
    cur = con.cursor()
    cur.row_factory = None                      # bare tuples for _PrintRow
    prints = [_PrintRow(r) for r in cur.execute(
        f"SELECT {PRINT_COLS} FROM prints WHERE proxy_wallet = ? {PRINT_ORDER}",
        (wallet,))]
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
    cycle; `cap=0` means no bound (the forced rebuild), otherwise
    `whale.mat.max_wallets_per_cycle` wallets a call, oldest-dirty first.

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
    if todo:
        markets = {r["condition_id"]: dict(r)
                   for r in con.execute(f"SELECT {MARKET_COLS} FROM markets")}
        for wallet in todo:
            _wallet(con, cfg, wallet, markets)
            res["wallets"] += 1
    res["dirty"] = con.execute("SELECT COUNT(*) c FROM mat_dirty").fetchone()[0]
    res["rows"] = con.execute("SELECT COUNT(*) c FROM positions_mat").fetchone()[0]
    res["secs"] = round(time.monotonic() - t0, 3)
    return res


def load_positions(con) -> list[Position]:
    """The cache, as `positions_from_prints` would have returned it."""
    cur = con.cursor()
    cur.row_factory = None
    return [Position(
        wallet=r[0], condition_id=r[1], outcome_index=r[2], avg_price=Decimal(r[3]),
        size=Decimal(r[4]), notional_usd=Decimal(r[5]), entry_ts=r[6], last_ts=r[7],
        pregame=bool(r[8]), sport=r[9], market_type=r[10], root=r[11], source=r[12],
        sold_size=Decimal(r[13]), sell_value=Decimal(r[14]))
        for r in cur.execute(f"SELECT {POSITION_COLS} FROM positions_mat {POSITION_ORDER}")]


def load_hygiene(con) -> Hygiene:
    """The cache, as `hygiene_flags` would have returned it. A wallet with no
    flags simply has no row — `Hygiene.excludes` reads a missing key and an
    empty set the same way, so this is the same object for every caller."""
    wallets: dict[str, set[str]] = {}
    markets: dict[tuple[str, str], set[str]] = {}
    for wallet, cid, flag in con.execute(
            "SELECT proxy_wallet, condition_id, flag FROM hygiene_mat"):
        target = wallets.setdefault(wallet, set()) if cid is None \
            else markets.setdefault((wallet, cid), set())
        target.add(flag)
    return Hygiene(wallets=wallets, markets=markets)
