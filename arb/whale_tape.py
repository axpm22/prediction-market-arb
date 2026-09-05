"""L14 whale-consensus collector: Polymarket prints -> its OWN SQLite file.

Rule 1: public, unauthenticated GETs only. Nothing here places an order.
Rule 3: every endpoint and field below was verified live and dated in
        data/verified_facts.md BEFORE this module was written. Task 3's own
        step 0 (2026-09-03) established the three facts it leans on: the
        global /trades feed is NEWEST-FIRST, `offset` continues that same
        feed, and `start`/`end` do filter — which the collector still does
        not use (spec §5 step 2: filter client-side, page by offset, age).
Rule 5: price/size/notional are 6-place Decimal strings (arb.models.q6).
Rule 9: writes only to whale.tape_db, so flat_bets is structurally
        unreachable from this lane — the arb/pm_tape.py defence.

I/O only. Positions, scores and signals are pure functions elsewhere.
One print row (parse + persist) lives in arb/whale_rows.py, the market
universe in arb/whale_universe.py, and the derived positions/hygiene cache's
own DDL in arb/whale_mat.py (MAT_SCHEMA, appended by `open_tape`) — all three
on size alone.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arb.logutil import get_logger
from arb.venues.base import get_json, VenueError
from arb.whale_rows import _keep
# The universe reader and the tape's `markets` table live in their own module
# now (size only). `_iso` is re-exported: arb/whale_resolve.py imports the
# collector's shared helpers from here, as it always has.
from arb.whale_universe import _iso, persist_markets  # noqa: F401
# I2 (review of a90a08d/fce71ea): the migration guard moved out to keep this
# module from growing past its size budget — re-exported so `whale_tape._migrate`
# stays the call every existing reference (open_tape, tests) uses.
from arb.whale_migrate import _MIGRATIONS, _migrate  # noqa: F401

log = get_logger("arb.whale", "logs/whale.log")

DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
# Endpoint shape, not tunables: 50 is the leaderboard page size probed in
# Task 1 (spec §14's leaderboard block deliberately has no key for it).
# GAMMA_IDS_PER_CALL=20 is the largest condition_ids batch VERIFIED (legacy
# re-map, 2026-09-04, data/verified_facts.md last row): n=2/10/20 all
# matched fully live; n=50 silently CAPPED at 20 rows (20/50 matched, no
# error) — 20 is a hard ceiling, not a guess (rule 3), and `_gamma_url`
# below refuses anything past it.
LEADERBOARD_PAGE = 50
GAMMA_IDS_PER_CALL = 20
SPORTS_MARKET_TYPE = {"moneyline": "WINNER", "totals": "TOTAL"}


def _gamma_url(ids, closed=False) -> str:
    """Gamma's condition_ids takes the REPEATED-param form only (verified
    2026-09-03; the comma CSV silently matches nothing) — one builder shared
    by every Gamma condition_ids caller (whale_resolve's resolution reader,
    whale_map's market mapper) so the query string is written once.
    `closed=true` is the only query verified to reach a resolved/archived
    market; the default index omits it (F0, mapper fix pass 2 2026-09-04 —
    live probe in data/verified_facts.md: 0/2 without it, 2/2 with it).

    A batch over GAMMA_IDS_PER_CALL is refused (legacy re-map, 2026-09-04):
    Gamma silently caps a 50-id call at 20 rows with no error, so a caller
    that forgot to chunk would count real markets as absent — an id count
    check here is the only thing standing between that and dropped markets,
    since Gamma itself gives no signal."""
    if len(ids) > GAMMA_IDS_PER_CALL:
        raise ValueError(f"Gamma condition_ids batch of {len(ids)} exceeds "
                         f"GAMMA_IDS_PER_CALL={GAMMA_IDS_PER_CALL} — a bigger batch "
                         "silently drops markets (data/verified_facts.md)")
    url = GAMMA + "/markets?" + "&".join(f"condition_ids={c}" for c in ids)
    return f"{url}&closed=true" if closed else url


SCHEMA = """
-- prints columns/INSERT: arb/whale_rows.py PRINT_COLS (kept in sync by hand).
CREATE TABLE IF NOT EXISTS prints (
    id INTEGER PRIMARY KEY, observed_at TEXT, ts INTEGER, tx_hash TEXT,
    proxy_wallet TEXT, pseudonym TEXT, condition_id TEXT, asset TEXT, outcome TEXT,
    outcome_index INTEGER, side TEXT, price TEXT, size TEXT, notional_usd TEXT,
    event_slug TEXT, slug TEXT, title TEXT, source TEXT, raw_json TEXT);
-- Every leg of a fill is its own row under `taker_only: false` (2026-09-04):
-- a maker leg carries the MAKER's wallet and side, so one transactionHash's
-- legs differ in proxy_wallet (144/144 fills in the live probe) and all
-- survive this key. Two maker legs of the SAME wallet/side/price/size/ts in
-- one tx DO collide and only one is stored — accepted: a leg index in the key
-- would break the overlap window's dedup (the only defence against
-- double-counting a re-fetched page), and under-counting a whale's size is
-- the conservative direction for both `left` tests.
CREATE UNIQUE INDEX IF NOT EXISTS idx_prints_key
    ON prints(tx_hash, proxy_wallet, asset, side, price, size, ts);
CREATE INDEX IF NOT EXISTS idx_prints_market ON prints(condition_id, ts);
-- posperf (2026-09-04): whale_score.positions_from_prints groups by
-- (proxy_wallet, condition_id, outcome_index) and needs each group's rows in
-- ascending ts. Neither existing index covers that; EXPLAIN QUERY PLAN on
-- `SELECT ... FROM prints ORDER BY proxy_wallet, condition_id, outcome_index,
-- ts` without this index still used idx_prints_wallet for the scan but added
-- "USE TEMP B-TREE FOR LAST 3 TERMS OF ORDER BY". With it, the same query is
-- a single `SCAN prints USING INDEX idx_prints_score` — arb/whale_state.py's
-- score_state now asks for exactly this order, so positions_from_prints no
-- longer has to sort the whole tape in Python. CREATE INDEX IF NOT EXISTS is
-- self-migrating (open_tape's executescript runs it on every open, old tapes
-- included) — no arb/whale_migrate.py entry needed, unlike an ADD COLUMN.
CREATE INDEX IF NOT EXISTS idx_prints_score
    ON prints(proxy_wallet, condition_id, outcome_index, ts);
CREATE INDEX IF NOT EXISTS idx_prints_wallet ON prints(proxy_wallet, ts);
-- `volume_24h` (non-sports roots amendment 2026-09-04): the venue's own 24h
-- volume, V6's denominator. The lane reads it from the ledger's watch quotes
-- for a sports market, but a non-sports market has no watch quote at all, and
-- an unknown volume reads as `dominance.min_volume_usd` ($5,000) — which on a
-- $20M/day politics market would make every signal maximally dominant. So the
-- Gamma figure is stored here when a Gamma call already answered it.
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY, sport TEXT, market_type TEXT, line TEXT,
    start_utc TEXT, event_key TEXT, tokens_json TEXT, slug TEXT,
    first_seen TEXT, last_seen TEXT, volume_24h TEXT);
CREATE TABLE IF NOT EXISTS wallets (
    proxy_wallet TEXT PRIMARY KEY, pseudonym TEXT, first_seen TEXT, source TEXT,
    backfilled_at TEXT, hygiene_flag TEXT, hygiene_at TEXT);
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    id INTEGER PRIMARY KEY, snapshot_at TEXT, category TEXT, time_period TEXT,
    order_by TEXT, rank INTEGER, proxy_wallet TEXT, user_name TEXT, vol TEXT, pnl TEXT);
CREATE INDEX IF NOT EXISTS idx_leaderboard_snapshots_at
    ON leaderboard_snapshots(snapshot_at);
CREATE TABLE IF NOT EXISTS resolutions (
    condition_id TEXT PRIMARY KEY, resolved_at TEXT, winner_outcome_index INTEGER,
    status TEXT, source TEXT, raw_json TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY, evaluated_at TEXT, condition_id TEXT, side TEXT, variant TEXT,
    points_yes TEXT, points_no TEXT, ratio TEXT, whale_ids_json TEXT, vwap TEXT,
    touch_ask TEXT, touch_bid TEXT, depth_usd TEXT, fired INTEGER, suppressed_reason TEXT,
    bet_ids_json TEXT, flagged_edge TEXT, holders_ratio TEXT);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(evaluated_at);
-- holdings columns/INSERT: arb/whale_holdings.py COLS (kept in sync by hand,
-- like `prints`). One row per open position in a wallet's `positions?user=`
-- snapshot (§9 item C, 2026-09-04); the poll ATTEMPT is meta
-- `holdings_at:<wallet>`, so a wallet holding nothing is still known to have
-- been read.
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY, observed_at TEXT, proxy_wallet TEXT, condition_id TEXT,
    asset TEXT, outcome_index INTEGER, size TEXT, avg_price TEXT,
    total_bought TEXT, redeemable INTEGER, raw_json TEXT);
CREATE INDEX IF NOT EXISTS idx_holdings_wallet
    ON holdings(proxy_wallet, observed_at);
-- holdings_snapshots columns/INSERT: arb/whale_holdings.py SNAP_COLS. One row
-- per wallet POLL (retention addendum 2026-09-04): `holdings` above now keeps
-- only the positions in a market this tape tracks, so this is where the
-- whole-page fact survives (rule 7) — how many positions the wallet held and
-- what they were worth, whether or not any of them were ours.
CREATE TABLE IF NOT EXISTS holdings_snapshots (
    id INTEGER PRIMARY KEY, observed_at TEXT, proxy_wallet TEXT,
    n_positions INTEGER, total_value_usd TEXT, partial INTEGER);
CREATE INDEX IF NOT EXISTS idx_holdings_snapshots_wallet
    ON holdings_snapshots(proxy_wallet, observed_at);
CREATE TABLE IF NOT EXISTS base_rates (
    root TEXT, as_of TEXT, value TEXT, n INTEGER, PRIMARY KEY (root, as_of));
-- V5's percentile bar, per bucket per cycle (§7 V5, 2026-09-04): the cutoff
-- and the size of the population behind it. The elite set moves week to week,
-- so the bar itself has to be on the record — arb/whale_elite.py.
CREATE TABLE IF NOT EXISTS elite_cutoffs (
    root TEXT, bucket TEXT, as_of TEXT, cutoff TEXT, n INTEGER,
    PRIMARY KEY (bucket, as_of));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

def open_tape(path: str | Path) -> sqlite3.Connection:
    """WAL + busy_timeout: `whale` and `settle` both touch this file (Task 4)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    # matpos (2026-09-05): the derived positions/hygiene cache's own DDL lives
    # with the only module that reads or writes it (arb/whale_mat.py MAT_SCHEMA)
    # — this file is already over its ~300-line budget, which is why
    # arb/whale_migrate.py was split out of it too. Imported inside the call so
    # neither module has to import the other at module scope.
    from arb.whale_mat import MAT_SCHEMA
    con.executescript(SCHEMA + MAT_SCHEMA)
    _migrate(con)
    con.commit()
    return con


class _Bucket:
    """Token bucket, whale.max_calls_per_min. Starts full, so a short cycle
    never sleeps and only the long backfill ladder is paced."""

    def __init__(self, per_min):
        self.rate = float(per_min) / 60.0
        self.cap = self.tokens = float(per_min)
        self.t = time.monotonic()

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens < 1.0:
            time.sleep((1.0 - self.tokens) / self.rate)
            self.tokens = 1.0
        self.tokens -= 1.0


_BUCKET: _Bucket | None = None


def _bucket(cfg) -> _Bucket:
    """ONE bucket per process, shared by poll_prints / snapshot_leaderboard /
    backfill: a bucket per call site let one command burst 3x the cap.
    # ponytail: first cfg wins for the life of the process — max_calls_per_min
    # is not reloaded mid-run anywhere, and the supervisor restarts on change."""
    global _BUCKET
    if _BUCKET is None:
        _BUCKET = _Bucket(cfg["whale"]["max_calls_per_min"])
    return _BUCKET


def _call(bucket, get, url, cfg, res) -> list:
    """One paced GET. get_json returns parsed JSON or raises VenueError whose
    message carries the HTTP status, so the status IS the exception text.

    A 200 whose body is not a list (an error object from a degraded Gamma,
    e.g.) is itself a failure, not an empty result: it used to be silently
    coerced to `[]` with status_counts still "ok", which reads to a caller
    exactly like a genuine "found nothing" — in whale_map._map_markets that
    made a degraded response on both Gamma passes write a PERMANENT
    `absent:<cond>` marker, quarantining a real market forever with nothing
    logged above INFO. Raising VenueError here — the one shared choke point
    for the sweep, the resolutions reader, and the mapper — routes it
    through the exact handling a bad HTTP status already gets (backoff set,
    status="error", row untouched) instead of needing a second check in
    every caller."""
    bucket.take()
    res["calls"] = res.get("calls", 0) + 1
    counts = res.setdefault("status_counts", {})
    t0 = time.monotonic()
    try:
        body = get(url, cfg)
        if not isinstance(body, list):
            raise VenueError(f"whale GET 200 non-list body ({type(body).__name__}): {url}")
    except Exception as e:
        counts["error"] = counts.get("error", 0) + 1
        log.warning("whale GET failed in %dms: %s (%s)",
                    int((time.monotonic() - t0) * 1000), url, e)
        raise
    counts["ok"] = counts.get("ok", 0) + 1
    log.info("whale GET 200 %dms n=%d %s", int((time.monotonic() - t0) * 1000), len(body), url)
    return body


def _meta(con, key):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(con, key, value):
    con.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    con.commit()


def _backoff_active(con, now) -> bool:
    until = _meta(con, "backoff_until")
    return bool(until and datetime.fromisoformat(until) > now)


def _set_backoff(con, cfg, now):
    """The pause runs from the WALL clock at the moment of failure, not from
    the cycle-start `now` the caller carries: a backfill cycle runs ~11 min
    against backoff_secs=300, so `now` would write an already-expired pause.
    Never earlier than `now` either, so a replayed/fixed clock still pauses."""
    wall = datetime.now(timezone.utc) if now.tzinfo else datetime.now()
    until = max(now, wall) + timedelta(seconds=int(cfg["whale"]["backoff_secs"]))
    _set_meta(con, "backoff_until", until.isoformat())
    log.warning("whale collector backing off until %s", until.isoformat())


def _json_list(value) -> list:
    """Gamma JSON-encodes clobTokenIds/outcomes as STRINGS, not arrays."""
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def poll_prints(cfg, con, markets, now, get=get_json) -> dict:
    """One collector cycle (spec §5 step 2). Global mode: one marketless call,
    rows kept through `whale_rows.doors` (universe in full, the rest above the
    global floor and compact — amendment 2026-09-04), paging by `offset` only
    while a page is full AND its oldest row is newer than last_seen - overlap."""
    w = cfg["whale"]
    res = {"calls": 0, "rows": 0, "dupes": 0, "bad_rows": 0, "gap_secs": 0,
           "status_counts": {}, "status": "ok"}
    if _backoff_active(con, now):
        res["status"] = "backoff"
        return res
    persist_markets(con, markets, now)
    ids = {m.condition_id for m in markets}
    bucket = _bucket(cfg)
    taker = "true" if w["taker_only"] else "false"
    size = int(w["page_size"])
    newest = 0
    try:
        if str(w["collector_mode"]) == "batched":
            ordered = [m.condition_id for m in markets]
            per = int(w["markets_per_call"])
            for i in range(0, len(ordered), per):
                csv = ",".join(ordered[i:i + per])   # comma CSV verified for /trades
                body = _call(bucket, get, f"{DATA_API}/trades?market={csv}"
                             f"&takerOnly={taker}&limit={size}", cfg, res)
                newest = max([newest] + _keep(con, body, ids, now, res, cfg=cfg))
        else:
            # No last_seen => cold start, no gap to catch up on: one page and
            # stop, or the age rule would walk the venue's whole history.
            last_seen = int(_meta(con, "last_seen_ts") or 0)
            floor = last_seen - int(w["overlap_secs"])
            cap, offset, oldest = int(w["max_offset"]), 0, None
            while True:
                url = f"{DATA_API}/trades?takerOnly={taker}&limit={size}"
                body = _call(bucket, get, url + (f"&offset={offset}" if offset else ""),
                             cfg, res)
                stamps = _keep(con, body, ids, now, res, cfg=cfg)
                # A body with rows but zero parsed timestamps is an unreadable
                # page, not a quiet end of the feed: `not stamps` below would
                # otherwise read as "caught up" and silently advance nothing,
                # write no backoff, and leave the gap behind it unrevisited.
                if body and not stamps:
                    raise VenueError(f"whale poll: {len(body)} rows, 0 parsed")
                newest = max([newest] + stamps)
                if stamps:
                    oldest = min(stamps) if oldest is None else min(oldest, *stamps)
                if len(body) < size or not stamps or not last_seen or min(stamps) <= floor:
                    break
                if offset + size > cap:
                    # The feed 400s past max_offset (observed 2026-09-04): the
                    # rows behind this page are unreachable, so record the span
                    # we will never fetch and move past it. Walking back into
                    # that 400 every cycle is how the collector wedged (Task 8).
                    if oldest > last_seen:   # else the overlap already covers it
                        res["gap_secs"] = oldest - last_seen
                        _set_meta(con, "last_gap", json.dumps(
                            {"from_ts": last_seen, "to_ts": oldest,
                             "secs": res["gap_secs"], "at": now.isoformat()}))
                        log.warning("whale poll capped at offset=%d (max_offset=%d):"
                                    " %ds of prints unfetched, recorded as a gap",
                                    offset, cap, res["gap_secs"])
                    break
                offset += size
    except Exception as e:
        log.warning("whale poll aborted after %d call(s): %s", res["calls"], e)
        _set_backoff(con, cfg, now)
        res["status"] = "error"
    # ONLY on a completed poll: on an abort the pages behind the failure were
    # never fetched, and advancing the watermark would strand that gap forever.
    # A CAPPED poll is completed, and its watermark is the newest row fetched
    # like any other cycle (§5 step 2, re-amended 2026-09-04): the unreachable
    # span below it is recorded ONCE as meta.last_gap. Stopping at the oldest
    # row instead left the next floor two pages behind a `now` that had moved
    # on, so the cap - and a fresh spurious gap - fired again every cycle.
    if newest and res["status"] == "ok":
        _set_meta(con, "last_seen_ts", newest)
    return res
