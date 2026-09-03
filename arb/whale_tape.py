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
One print row (parse + persist) lives in arb/whale_rows.py, on size alone.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arb.logutil import get_logger
from arb.venues.base import get_json, VenueError
from arb.whale_rows import _keep

log = get_logger("arb.whale", "logs/whale.log")

DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
# Endpoint shape, not tunables: 50 is the leaderboard page size probed in
# Task 1 (spec §14's leaderboard block deliberately has no key for it), and 2
# is the ONLY gamma condition_ids batch size verified — a larger cap was NOT
# PROBED and rule 3 forbids guessing one.
LEADERBOARD_PAGE = 50
GAMMA_IDS_PER_CALL = 2
SPORTS_MARKET_TYPE = {"moneyline": "WINNER", "totals": "TOTAL"}

SCHEMA = """
-- prints columns/INSERT: arb/whale_rows.py PRINT_COLS (kept in sync by hand).
CREATE TABLE IF NOT EXISTS prints (
    id INTEGER PRIMARY KEY, observed_at TEXT, ts INTEGER, tx_hash TEXT,
    proxy_wallet TEXT, pseudonym TEXT, condition_id TEXT, asset TEXT, outcome TEXT,
    outcome_index INTEGER, side TEXT, price TEXT, size TEXT, notional_usd TEXT,
    event_slug TEXT, slug TEXT, title TEXT, source TEXT, raw_json TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prints_key
    ON prints(tx_hash, proxy_wallet, asset, side, price, size, ts);
CREATE INDEX IF NOT EXISTS idx_prints_market ON prints(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_prints_wallet ON prints(proxy_wallet, ts);
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY, sport TEXT, market_type TEXT, line TEXT,
    start_utc TEXT, event_key TEXT, tokens_json TEXT, slug TEXT,
    first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS wallets (
    proxy_wallet TEXT PRIMARY KEY, pseudonym TEXT, first_seen TEXT, source TEXT,
    backfilled_at TEXT, hygiene_flag TEXT, hygiene_at TEXT);
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    id INTEGER PRIMARY KEY, snapshot_at TEXT, category TEXT, time_period TEXT,
    order_by TEXT, rank INTEGER, proxy_wallet TEXT, user_name TEXT, vol TEXT, pnl TEXT);
CREATE TABLE IF NOT EXISTS resolutions (
    condition_id TEXT PRIMARY KEY, resolved_at TEXT, winner_outcome_index INTEGER,
    status TEXT, source TEXT, raw_json TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY, evaluated_at TEXT, condition_id TEXT, side TEXT, variant TEXT,
    points_yes TEXT, points_no TEXT, ratio TEXT, whale_ids_json TEXT, vwap TEXT,
    touch_ask TEXT, touch_bid TEXT, depth_usd TEXT, fired INTEGER, suppressed_reason TEXT,
    bet_ids_json TEXT, flagged_edge TEXT);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(evaluated_at);
CREATE TABLE IF NOT EXISTS base_rates (
    root TEXT, as_of TEXT, value TEXT, n INTEGER, PRIMARY KEY (root, as_of));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# `slug` is kept so a market Gamma answered for but whose league code is not
# in whale.slug_sport_prefixes can be re-mapped LOCALLY when that map grows —
# without it, an unmappable market would be stuck at sport=NULL forever.
MARKET_COLS = ("condition_id, sport, market_type, line, start_utc, event_key,"
               " tokens_json, slug, first_seen, last_seen")
# The ledger's PM sports quotes for ONE poll: served by idx_quotes_poll. A
# fetched_at range instead would scan the 37 GB table.
UNIVERSE_SQL = (
    "SELECT venue_market_id, sport, market_type, line, outcome, start_utc, event_key "
    "FROM quotes WHERE poll_id = (SELECT MAX(id) FROM polls WHERE kind='watch') "
    "AND venue='polymarket' AND category='sports'")


def open_tape(path: str | Path) -> sqlite3.Connection:
    """WAL + busy_timeout: `whale` and `settle` both touch this file (Task 4)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    con.commit()
    return con


@dataclass(frozen=True)
class Market:
    condition_id: str
    sport: str | None
    market_type: str | None
    line: str | None
    start_utc: str | None
    event_key: str | None
    tokens: tuple = ()          # ((token_id, outcome_label), ...)


def universe(store_ro, cfg, now) -> list[Market]:
    """Polymarket sports markets from the LATEST watch poll whose start lies in
    [now - lookback_hours, now + lookahead_hours] (spec §5 step 1)."""
    w = cfg["whale"]
    lo = now - timedelta(hours=float(w["lookback_hours"]))
    hi = now + timedelta(hours=float(w["lookahead_hours"]))
    by_cond: dict[str, dict] = {}
    for r in store_ro.execute(UNIVERSE_SQL):
        vmid = r["venue_market_id"] or ""
        start = _iso(r["start_utc"])
        if ":" not in vmid or start is None or not (lo <= datetime.fromisoformat(start) <= hi):
            continue
        cond, token = vmid.split(":", 1)
        m = by_cond.setdefault(cond, {"sport": r["sport"], "market_type": r["market_type"],
                                      "line": r["line"], "start_utc": start,
                                      "event_key": r["event_key"], "tokens": {}})
        m["tokens"][token] = r["outcome"]
    return [Market(condition_id=c, sport=m["sport"], market_type=m["market_type"],
                   line=m["line"], start_utc=m["start_utc"], event_key=m["event_key"],
                   tokens=tuple(sorted(m["tokens"].items()))) for c, m in by_cond.items()]


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
    message carries the HTTP status, so the status IS the exception text."""
    bucket.take()
    res["calls"] = res.get("calls", 0) + 1
    counts = res.setdefault("status_counts", {})
    t0 = time.monotonic()
    try:
        body = get(url, cfg)
    except Exception as e:
        counts["error"] = counts.get("error", 0) + 1
        log.warning("whale GET failed in %dms: %s (%s)",
                    int((time.monotonic() - t0) * 1000), url, e)
        raise
    counts["ok"] = counts.get("ok", 0) + 1
    n = len(body) if isinstance(body, list) else 0
    log.info("whale GET 200 %dms n=%d %s", int((time.monotonic() - t0) * 1000), n, url)
    return body if isinstance(body, list) else []


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


def _iso(value) -> str | None:
    """Normalise a venue timestamp to the ledger's own ISO form. Gamma sends
    '2026-08-09 09:00:00+00', which SQLite's strftime cannot parse."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def persist_markets(con, markets, now):
    con.executemany(
        f"INSERT INTO markets ({MARKET_COLS}) VALUES ({','.join('?' * 10)}) "
        "ON CONFLICT(condition_id) DO UPDATE SET last_seen=excluded.last_seen, "
        "sport=COALESCE(excluded.sport, sport), "
        "market_type=COALESCE(excluded.market_type, market_type), "
        "start_utc=COALESCE(excluded.start_utc, start_utc), "
        "tokens_json=excluded.tokens_json",
        [(m.condition_id, m.sport, m.market_type, m.line, m.start_utc, m.event_key,
          json.dumps([list(t) for t in m.tokens]), None, now.isoformat(), now.isoformat())
         for m in markets])
    con.commit()


def poll_prints(cfg, con, markets, now, get=get_json) -> dict:
    """One collector cycle (spec §5 step 2). Global mode: one marketless call,
    kept rows filtered client-side, paging by `offset` only while a page is
    full AND its oldest row is still newer than last_seen - overlap_secs."""
    w = cfg["whale"]
    res = {"calls": 0, "rows": 0, "dupes": 0, "bad_rows": 0,
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
                newest = max([newest] + _keep(con, body, ids, now, res))
        else:
            # No last_seen => cold start, no gap to catch up on: one page and
            # stop, or the age rule would walk the venue's whole history.
            last_seen = int(_meta(con, "last_seen_ts") or 0)
            floor = last_seen - int(w["overlap_secs"])
            offset = 0
            # ponytail: the catch-up walk after an outage is bounded only by
            # the token bucket — at ~1,515 rows/min a 24h gap is ~218 pages of
            # 10,000, i.e. ~7 min at max_calls_per_min=30, and `offset` beyond
            # the verified 10,000 is UNPROBED. Cap it (or use the now-verified
            # start/end window) if an outage ever outruns a cycle.
            while True:
                url = f"{DATA_API}/trades?takerOnly={taker}&limit={size}"
                body = _call(bucket, get, url + (f"&offset={offset}" if offset else ""),
                             cfg, res)
                stamps = _keep(con, body, ids, now, res)
                # A body with rows but zero parsed timestamps is an unreadable
                # page, not a quiet end of the feed: `not stamps` below would
                # otherwise read as "caught up" and silently advance nothing,
                # write no backoff, and leave the gap behind it unrevisited.
                if body and not stamps:
                    raise VenueError(f"whale poll: {len(body)} rows, 0 parsed")
                newest = max([newest] + stamps)
                if len(body) < size or not stamps or not last_seen or min(stamps) <= floor:
                    break
                offset += size
    except Exception as e:
        log.warning("whale poll aborted after %d call(s): %s", res["calls"], e)
        _set_backoff(con, cfg, now)
        res["status"] = "error"
    # ONLY on a completed poll: on an abort the pages behind the failure were
    # never fetched, and advancing the watermark would strand that gap forever.
    if newest and res["status"] == "ok":
        _set_meta(con, "last_seen_ts", newest)
    return res
