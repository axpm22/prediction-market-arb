"""Retrospective Kalshi settled-market corpus — the free, keyless dataset that
STRATEGIES.md L6 (near-certainty tail rate) and L8 (calibration harvests) need
to reach decision-grade power today instead of in six months.

Rule 1: public unauthenticated GETs only, market data only.
Rule 2: no Odds API credits are spent here — Kalshi polling is credit-free.
Rule 3: every endpoint below was verified live 2026-07-27 and dated in
        data/verified_facts.md before this module was written.
Rule 9: writes to its OWN SQLite file, so nothing here can reach flat_bets.

The corpus is a cache, not a ledger: re-running `pull` is idempotent
(INSERT OR REPLACE keyed on ticker) and resumable — the expensive leg is one
candlestick request per market, and markets already carrying candles are
skipped.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from arb.logutil import get_logger
from arb.venues.base import VenueError, get_json
from arb.venues.kalshi import BASE

log = get_logger("arb.kalshi_history", "logs/scan.log")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_series (
    series_ticker TEXT PRIMARY KEY, category TEXT,
    n_events INTEGER, discovered_at TEXT);
CREATE TABLE IF NOT EXISTS hist_markets (
    ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT,
    category TEXT, title TEXT, result TEXT, last_price TEXT, volume TEXT,
    open_interest TEXT, open_time TEXT, close_time TEXT, pulled_at TEXT);
CREATE INDEX IF NOT EXISTS idx_hist_markets_series ON hist_markets(series_ticker);
CREATE INDEX IF NOT EXISTS idx_hist_markets_event ON hist_markets(event_ticker);
CREATE TABLE IF NOT EXISTS hist_candles (
    ticker TEXT PRIMARY KEY, period_mins INTEGER, n INTEGER,
    candles_json TEXT, pulled_at TEXT);
"""

# The tape lives in its OWN file. SQLite allows one writer per database, and
# the candle pull holds a write transaction across each 200-market batch, so a
# concurrent tape writer on the same file dies on "database is locked" (observed
# 2026-07-27). Separate files let the two pulls run at once; the bench ATTACHes
# the tape read-only, so `hist_trades` must NOT exist in the corpus schema or it
# would shadow the attached one.
TAPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_trades (
    ticker TEXT PRIMARY KEY, n INTEGER, trades_json TEXT, pulled_at TEXT);
"""


def open_db(path: str | Path, schema: str = SCHEMA) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(schema)
    return con


def open_ro(path: str | Path) -> sqlite3.Connection:
    """Read-only handle for the scorers (bench never writes the corpus)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pace(cfg) -> None:
    """Kalshi publishes no unauth rate limit; verified_facts assumes <=20 req/s.
    We pace far under that — this is a bulk backfill against someone else's free
    endpoint, and get_json already backs off on the 429s that slip through."""
    time.sleep(float(cfg["bench"]["request_pace_secs"]))


def _paged(cfg, url: str, key: str, max_pages: int):
    """Cursor pagination shared by the events and markets listings."""
    cursor = ""
    for _ in range(max_pages):
        page = get_json(url + (f"&cursor={cursor}" if cursor else ""), cfg)
        rows = page.get(key) or []
        yield rows
        cursor = page.get("cursor") or ""
        if not cursor or not rows:
            return
        _pace(cfg)


def discover_series(cfg, con) -> int:
    """Walk settled EVENTS to learn which series exist and, critically, their
    `category` — the market object does not carry one, and L8's pre-registered
    grid is category-stratified. Verified 2026-07-27:
    GET /events?status=settled&limit=200 -> 200, cursor-paginated."""
    b = cfg["bench"]
    counts: dict[str, int] = {}
    cats: dict[str, str] = {}
    for rows in _paged(cfg, f"{BASE}/events?status=settled&limit=200",
                       "events", int(b["max_event_pages"])):
        for e in rows:
            s = e.get("series_ticker")
            if not s:
                continue
            counts[s] = counts.get(s, 0) + 1
            cats[s] = e.get("category") or "Uncategorized"
    con.executemany(
        "INSERT OR REPLACE INTO hist_series VALUES (?,?,?,?)",
        [(s, cats[s], n, _now()) for s, n in counts.items()])
    con.commit()
    log.info("kalshi_history: discovered %s series across %s categories",
             len(counts), len(set(cats.values())))
    return len(counts)


def selected_series(cfg, con) -> list[sqlite3.Row]:
    """Top-N series per category by settled-event count. Stratifying the cap by
    category (rather than taking a global top-N) is what stops the corpus from
    collapsing into the sports monoculture the global listing is made of."""
    cap = int(cfg["bench"]["max_series_per_category"])
    out = []
    cats = [r["category"] for r in con.execute(
        "SELECT DISTINCT category FROM hist_series ORDER BY category")]
    for c in cats:
        out += con.execute(
            "SELECT * FROM hist_series WHERE category=? "
            "ORDER BY n_events DESC LIMIT ?", (c, cap)).fetchall()
    return out


def pull_markets(cfg, con, series_ticker: str, category: str) -> int:
    """Settled markets for one series. Verified 2026-07-27:
    GET /markets?status=settled&series_ticker=&limit=1000 -> 200 with `result`
    ('yes'/'no'), `last_price_dollars`, `volume_fp`, open/close times."""
    rows_out = []
    for markets in _paged(
            cfg, f"{BASE}/markets?status=settled&series_ticker={series_ticker}&limit=1000",
            "markets", int(cfg["bench"]["max_market_pages"])):
        for m in markets:
            rows_out.append((
                m["ticker"], m.get("event_ticker"), series_ticker, category,
                m.get("title"), m.get("result"), m.get("last_price_dollars"),
                m.get("volume_fp"), m.get("open_interest_fp"),
                m.get("open_time"), m.get("close_time"), _now()))
    con.executemany(
        "INSERT OR REPLACE INTO hist_markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows_out)
    con.commit()
    return len(rows_out)


def candle_targets(cfg, con) -> list[sqlite3.Row]:
    """Markets worth one candlestick request each: settled to a real outcome,
    actually traded, and not already cached. Never-traded markets carry no
    price to calibrate and would burn the request budget on noise.

    The per-series cap is the load-bearing part. A global volume-DESC ORDER BY
    fills the whole budget with KXBTC15M — Kalshi's 15-minute crypto series
    out-trades everything else by volume and lives far too briefly to carry a
    24h-horizon price. Sampling top-N *within* each series buys the category
    and horizon spread that L8's pre-registered grid needs, which is the same
    universe-bias failure STRATEGIES.md L8 was promoted to escape.

    Within a series, long-lived markets are fetched first. A market that closes
    less than `horizon_hours` after it opened can never carry the T-delta price
    L8 needs, so it is worth one request only to L6/L7 — half the first pull's
    budget went to 15-minute markets that L8 then discarded. They are still
    fetched, just after the units every lane can use."""
    b = cfg["bench"]
    return con.execute(
        "WITH ranked AS ("
        "  SELECT m.ticker, m.series_ticker, m.open_time, m.close_time,"
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY m.series_ticker"
        "           ORDER BY (julianday(m.close_time) - julianday(m.open_time))"
        "                    >= ? DESC, CAST(m.volume AS REAL) DESC) rn"
        "  FROM hist_markets m LEFT JOIN hist_candles c USING (ticker)"
        "  WHERE c.ticker IS NULL AND m.result IN ('yes','no')"
        "    AND m.open_time IS NOT NULL AND m.close_time IS NOT NULL"
        "    AND CAST(m.volume AS REAL) >= ?)"
        "SELECT ticker, series_ticker, open_time, close_time FROM ranked "
        "WHERE rn <= ? ORDER BY rn LIMIT ?",
        (int(b["horizon_hours"]) / 24.0, float(b["min_volume_fp"]),
         int(b["max_candle_per_series"]), int(b["max_candle_markets"]))).fetchall()


def pull_candles(cfg, con, targets: list[sqlite3.Row]) -> tuple[int, int]:
    """One candlestick series per market. Verified 2026-07-27: GET /series/{s}/
    markets/{t}/candlesticks?start_ts=&end_ts=&period_interval=60 -> 200, each
    period carrying trade OHLC plus yes_ask/yes_bid OHLC. The ask leg is the
    reason this endpoint is worth the requests: it is the only free source of
    an EXECUTABLE historical price (rule 1 of the measurement rules bans mids).
    """
    period = int(cfg["bench"]["candle_period_mins"])
    ok = err = 0
    for i, t in enumerate(targets, 1):
        try:
            start = int(datetime.fromisoformat(t["open_time"].replace("Z", "+00:00")).timestamp())
            end = int(datetime.fromisoformat(t["close_time"].replace("Z", "+00:00")).timestamp())
        except (ValueError, AttributeError):
            err += 1
            continue
        # Kalshi caps a candlestick request at max_candle_periods periods and
        # answers 400 above it — verified 2026-07-27: at period_interval=60 a
        # 208-day span returns 200 and 250 days returns 400 (= 5000 hours). A
        # long-lived market therefore yields its MOST RECENT window, which is
        # the half both consumers need: the T-delta horizon ask and the
        # late-life >=97c cohort both sit near close.
        start = max(start, end - int(cfg["bench"]["max_candle_periods"]) * period * 60)
        url = (f"{BASE}/series/{t['series_ticker']}/markets/{t['ticker']}"
               f"/candlesticks?start_ts={start}&end_ts={end}&period_interval={period}")
        try:
            data = get_json(url, cfg)
        except VenueError as e:
            err += 1
            log.warning("candles %s: %s", t["ticker"], e)
            _pace(cfg)
            continue
        candles = data.get("candlesticks") or []
        con.execute("INSERT OR REPLACE INTO hist_candles VALUES (?,?,?,?,?)",
                    (t["ticker"], period, len(candles), json.dumps(candles), _now()))
        ok += 1
        if i % 200 == 0:
            con.commit()
            log.info("kalshi_history: candles %s/%s", i, len(targets))
        _pace(cfg)
    con.commit()
    return ok, err


def pull(cfg, con, skip_discovery: bool = False) -> dict:
    """Full refresh: discover -> markets -> candles. Resumable; safe to re-run."""
    stats = {"series": 0, "markets": 0, "candles": 0, "candle_errors": 0}
    if not skip_discovery:
        stats["series"] = discover_series(cfg, con)
    for s in selected_series(cfg, con):
        stats["markets"] += pull_markets(cfg, con, s["series_ticker"], s["category"])
    targets = candle_targets(cfg, con)
    log.info("kalshi_history: %s markets, %s candle targets",
             stats["markets"], len(targets))
    stats["candles"], stats["candle_errors"] = pull_candles(cfg, con, targets)
    return stats


def trade_targets(cfg, con) -> list[sqlite3.Row]:
    """Markets worth one tape request. Ordered by volume because L11 asks
    whether SIZE predicts direction — a market with no large prints in it
    cannot answer that either way, so the budget goes where the prints are."""
    b = cfg["bench"]
    return con.execute(
        "SELECT m.ticker, m.event_ticker FROM hist_markets m "
        "LEFT JOIN tape.hist_trades t USING (ticker) "
        "WHERE t.ticker IS NULL AND m.result IN ('yes','no') "
        "  AND CAST(m.volume AS REAL) >= ? "
        "ORDER BY CAST(m.volume AS REAL) DESC LIMIT ?",
        (float(b["min_volume_fp"]), int(b["max_trade_markets"]))).fetchall()


def pull_trades(cfg, con, targets: list[sqlite3.Row]) -> tuple[int, int]:
    """One page of tape per market. Verified 2026-07-27: GET /markets/trades?
    ticker=&limit=1000 -> 200, keyless, cursor-paginated, newest-first, with
    `count_fp` (size), `yes_price_dollars`, `taker_side`, `is_block_trade`.

    ONE page deliberately: L11 needs enough prints per market to identify the
    large ones and follow what happened next, not the market's whole history —
    and paginating every market would multiply an already long pull by 5-10x
    against someone else's free endpoint."""
    ok = err = 0
    for i, t in enumerate(targets, 1):
        try:
            data = get_json(f"{BASE}/markets/trades?ticker={t['ticker']}&limit=1000", cfg)
        except VenueError as e:
            err += 1
            log.warning("trades %s: %s", t["ticker"], e)
            _pace(cfg)
            continue
        trades = data.get("trades") or []
        con.execute("INSERT OR REPLACE INTO tape.hist_trades VALUES (?,?,?,?)",
                    (t["ticker"], len(trades), json.dumps(trades), _now()))
        ok += 1
        if i % 50 == 0:
            # commit often: a reader blocked behind a long write transaction is
            # how the first tape run locked the bench out for minutes at a time
            con.commit()
            if i % 200 == 0:
                log.info("kalshi_history: trades %s/%s", i, len(targets))
        _pace(cfg)
    con.commit()
    return ok, err
