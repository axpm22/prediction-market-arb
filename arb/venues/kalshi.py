"""Kalshi adapter (SPEC §6.1). Public GETs only; decimal-dollar strings -> Decimal.

Verified facts (data/verified_facts.md): ticker-embedded YYMMMDDHHMM is the start
in America/New_York; occurrence_datetime is the expected END — never a start
source; close_time NEVER (bug D1). Volume field is volume_24h_fp. Orderbook is
orderbook_fp with [price, contracts] bid levels per side.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from arb.models import Quote
from arb.venues.base import get_json
from arb.venues.fees import leg_fee_cost

BASE = "https://api.elections.kalshi.com/trade-api/v2"
NY = ZoneInfo("America/New_York")
SERIES_CACHE = Path("data/kalshi_series.json")

# series scanned per canonical sport: (series_ticker, market_type)
SPORT_SERIES = {
    "baseball_mlb": [("KXMLBGAME", "WINNER"), ("KXMLBTOTAL", "TOTAL")],
    "basketball_nba": [("KXNBA", "WINNER"), ("KXNBATOTAL", "TOTAL")],
}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_START_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})[A-Z]")


def parse_start(ticker: str) -> datetime | None:
    """Ticker-embedded start, ET -> UTC. Unparseable -> None (start-unknown),
    NEVER close_time (bug D1)."""
    m = _START_RE.search(ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    yy, mon, dd, hh, mm = m.groups()
    try:
        local = datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hh), int(mm), tzinfo=NY)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def _teams_from_title(title: str) -> tuple[str, str]:
    base = re.sub(r"\s+(Winner|Total Runs|Total Points)\?*$", "", title.strip())
    parts = base.split(" vs ")
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", ""


def _empty_book(m: dict) -> bool:
    """Empty-book trap (§6.1): ask 1.0000 + bid 0.0000 + liquidity 0 is not a quote."""
    return (m.get("yes_ask_dollars") == "1.0000" and m.get("yes_bid_dollars") == "0.0000"
            and Decimal(m.get("liquidity_dollars") or "0") == 0)


def parse_markets(cfg, markets: list[dict], sport: str, market_type: str) -> list[Quote]:
    quotes: list[Quote] = []
    now = datetime.now(timezone.utc)
    for m in markets:
        if _empty_book(m):
            continue
        start = parse_start(m["ticker"])
        away, home = _teams_from_title(m.get("title", ""))
        vol = Decimal(m["volume_24h_fp"]) if m.get("volume_24h_fp") is not None else None
        if market_type == "TOTAL":
            line = str(Decimal(str(m["floor_strike"]))) if m.get("floor_strike") is not None else None
            outcome = "OVER"  # yes side buys Over floor_strike
        else:
            line = None
            outcome = m.get("yes_sub_title", "")
        common = dict(
            event_key="", venue="kalshi", book="kalshi", market_type=market_type,
            line=line, outcome_label=outcome, ladder_json=None, live=False,
            fetched_at=now, category="sports", sport=sport, volume_24h=vol,
            home=home, away=away, start_utc=start,
            start_grade="nominal" if start else "absent",
            venue_market_id=m["ticker"],
        )
        for side, ask_key, bid_key, size_key in (
                ("YES", "yes_ask_dollars", "yes_bid_dollars", "yes_ask_size_fp"),
                ("NO", "no_ask_dollars", "no_bid_dollars", "yes_bid_size_fp")):
            ask = Decimal(m[ask_key])
            size = m.get(size_key)
            depth = Decimal(size) * ask if size is not None else None
            quotes.append(Quote(
                side=side, raw_price=ask, cost=ask,
                fee_cost=leg_fee_cost(cfg, "kalshi", "sports", ask),
                bid=Decimal(m[bid_key]), depth_usd=depth, **common))
    return quotes


def fetch_markets(cfg) -> list[Quote]:
    quotes = []
    for sport in cfg["sports"]:
        for series, mtype in SPORT_SERIES.get(sport, []):
            cursor, pages = "", 0
            while pages < 10:
                url = f"{BASE}/markets?series_ticker={series}&status=open&limit=200"
                if cursor:
                    url += f"&cursor={cursor}"
                data = get_json(url, cfg)
                quotes.extend(parse_markets(cfg, data.get("markets", []), sport, mtype))
                cursor = data.get("cursor") or ""
                pages += 1
                if not cursor:
                    break
    return quotes


def fetch_orderbook(cfg, ticker: str) -> dict:
    """orderbook_fp: per-side BID levels [price_dollars, contracts]; the ask of one
    side derives as 1 - best opposite bid (verified vs market object 2026-07-08)."""
    return get_json(f"{BASE}/markets/{ticker}/orderbook", cfg).get("orderbook_fp", {})


def fetch_event(cfg, event_ticker: str) -> dict:
    """Event object; `mutually_exclusive` feeds the negRisk proof condition (a)."""
    return get_json(f"{BASE}/events/{event_ticker}", cfg).get("event", {})


def fetch_trades(cfg, ticker: str, limit: int = 100) -> list[dict]:
    return get_json(f"{BASE}/markets/trades?ticker={ticker}&limit={limit}", cfg).get("trades", [])


def refresh_series_cache(cfg) -> None:
    data = get_json(f"{BASE}/series?category=Sports", cfg)
    SERIES_CACHE.parent.mkdir(exist_ok=True)
    SERIES_CACHE.write_text(json.dumps(data, indent=1))
