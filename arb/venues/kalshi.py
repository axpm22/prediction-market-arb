from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from arb.config import AppConfig
from arb.matching import event_key, load_aliases, normalize_name
from arb.models import Quote
from arb.pricing import all_in_fee_cost
from arb.venues.slippage import effective_buffer

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_CACHE = Path("data/kalshi_series.json")
# Ticker timestamps (e.g. KXMLBGAME-26JUN101940TEXKC-TEX) are US Eastern wall-clock;
# verified against SX/Odds API start times June 2026. close_time is days AFTER the game.
TICKER_TZ = ZoneInfo("America/New_York")
TICKER_TIME = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})")
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
TITLE_SPLIT = re.compile(
    r"^(.+?)\s+(?:vs\.?|at)\s+(.+?)(?:\s+(?:Winner|Total(?:\s+\w+)?))?\?\s*$",
    flags=re.IGNORECASE,
)
OVER_SUBTITLE = re.compile(r"^Over\s+(\d+(?:\.\d+)?)", flags=re.IGNORECASE)
# Politeness limits for Kalshi's public API: it returns 429 on rapid orderbook reads.
MIN_REQUEST_INTERVAL_SECS = 0.15
MAX_RETRIES_429 = 4


class KalshiAdapter:
    name = "kalshi"

    def __init__(self, config: AppConfig, fixture_dir: Path | None = None) -> None:
        self.config = config
        self.fixture_dir = fixture_dir
        self.session = requests.Session()
        self._last_request_at = 0.0

    def fetch_quotes(self, sport: str) -> list[Quote]:
        if self.fixture_dir is not None:
            payload = json.loads((self.fixture_dir / f"kalshi_{sport}.json").read_text())
            return parse_kalshi_payload(payload, sport, self.config.kalshi_fee_rate, self.config.taker_price_buffer)
        if not SERIES_CACHE.exists():
            raise RuntimeError(f"Missing {SERIES_CACHE}; seed it from GET /series?category=Sports first.")
        series = json.loads(SERIES_CACHE.read_text()).get(sport)
        if not series:
            return []
        markets: list[dict[str, Any]] = []
        for ticker in [*series.get("winner", []), *series.get("totals", [])]:
            markets.extend(self._fetch_series_markets(ticker))
        orderbooks = {
            market["ticker"]: self._get(f"/markets/{market['ticker']}/orderbook")
            for market in markets
            if not _is_empty_book(market)
        }
        return parse_kalshi_payload({"markets": markets, "orderbooks": orderbooks}, sport,
                                    self.config.kalshi_fee_rate, self.config.taker_price_buffer,
                                    slippage_model=self.config.taker_slippage_model,
                                    slippage_ref_stake=self.config.slippage_ref_stake_usd)

    def _fetch_series_markets(self, series_ticker: str) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor = ""
        while True:
            path = f"/markets?series_ticker={series_ticker}&status=open&limit=200"
            if cursor:
                path += f"&cursor={cursor}"
            data = self._get(path)
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor") or ""
            if not cursor:
                return markets

    def _get(self, path: str) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES_429 + 1):
            wait = self._last_request_at + MIN_REQUEST_INTERVAL_SECS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            response = self.session.get(f"{BASE_URL}{path}", timeout=30)
            if response.status_code == 429 and attempt < MAX_RETRIES_429:
                time.sleep(float(response.headers.get("Retry-After") or 2 ** attempt))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Kalshi kept returning 429 for {path}")


def parse_kalshi_payload(
    payload: dict[str, Any] | list,
    sport: str,
    fee_rate: Decimal,
    taker_buffer: Decimal = Decimal("0"),
    slippage_model: str = "flat",
    slippage_ref_stake: Decimal = Decimal("50"),
) -> list[Quote]:
    if isinstance(payload, list):
        payload = {"markets": payload}
    markets = payload.get("markets", [])
    orderbooks = payload.get("orderbooks", {})
    fetched_at = datetime.now(timezone.utc)
    aliases = load_aliases()
    quotes: list[Quote] = []
    for market in markets:
        if _is_empty_book(market):
            continue
        teams = _teams_from_title(str(market.get("title", "")))
        if teams is None:
            continue
        home, away = teams
        start = _start_from_ticker(str(market["ticker"]))
        if start is None:
            # D1 (ARB_LADDER_AND_FRICTIONS_SPEC §4): NEVER fall back to close_time — it is days
            # after the game, so it corrupts the event_key date, every time guard, and the
            # settlement bucket. Leave start unset and tag the quote 'start-unknown' (non-
            # actionable); key on close_time's date for research display only — a different date
            # than the game means it will not match a real-start quote in detection.
            start_note: tuple[str, ...] = ("start-unknown",)
            key_basis = datetime.fromisoformat(str(market["close_time"]).replace("Z", "+00:00"))
            key = event_key(sport, home, away, key_basis, aliases)
        else:
            start_note = ()
            key = event_key(sport, home, away, start, aliases)
        book = orderbooks.get(str(market["ticker"]), {})
        subtitle = str(market.get("yes_sub_title", ""))
        over = OVER_SUBTITLE.match(subtitle)
        common = dict(
            event_key=key,
            venue="kalshi",
            book=str(market["ticker"]),
            live=False,
            fetched_at=fetched_at,
            start_utc=start,
            home=normalize_name(home, sport, aliases),
            away=normalize_name(away, sport, aliases),
            notes=start_note,
        )
        if over:
            line = Decimal(over.group(1))
            for label, side in [("OVER", "yes"), ("UNDER", "no")]:
                price = Decimal(str(market[f"{side}_ask_dollars"]))
                if price <= 0 or price >= 1:
                    continue
                ladder = _ladder_at_ask(book, side)
                buffer = effective_buffer(ladder, slippage_model, slippage_ref_stake, taker_buffer)
                quotes.append(
                    Quote(
                        market_type="TOTAL",
                        line=line,
                        outcome_label=label,
                        raw_price=price,
                        cost=price,
                        fee_cost=all_in_fee_cost(price, fee_rate, buffer, basis="quadratic"),
                        depth_usd=_depth_at_ask(book, side, price),
                        ladder=ladder,
                        **common,
                    )
                )
        else:
            price = Decimal(str(market["yes_ask_dollars"]))
            if price <= 0 or price >= 1:
                continue
            ladder = _ladder_at_ask(book, "yes")
            buffer = effective_buffer(ladder, slippage_model, slippage_ref_stake, taker_buffer)
            quotes.append(
                Quote(
                    market_type="WINNER",
                    line=None,
                    outcome_label=normalize_name(subtitle or home, sport, aliases),
                    raw_price=price,
                    cost=price,
                    fee_cost=all_in_fee_cost(price, fee_rate, buffer, basis="quadratic"),
                    depth_usd=_depth_at_ask(book, "yes", price),
                    ladder=ladder,
                    **common,
                )
            )
    return quotes


def _is_empty_book(market: dict[str, Any]) -> bool:
    # SPEC §3.1 trap: yes ask 1.0000 + yes bid 0.0000 + liquidity 0 = empty book, not a quote.
    ask = Decimal(str(market.get("yes_ask_dollars") or "1"))
    bid = Decimal(str(market.get("yes_bid_dollars") or "0"))
    liquidity = Decimal(str(market.get("liquidity_dollars") or "0"))
    return ask >= 1 and bid <= 0 and liquidity <= 0


def _start_from_ticker(ticker: str) -> datetime | None:
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    match = TICKER_TIME.match(parts[1])
    if not match:
        return None
    yy, mon, dd, hh, mm = match.groups()
    month = MONTHS.get(mon)
    if month is None:
        return None
    local = datetime(2000 + int(yy), month, int(dd), int(hh), int(mm), tzinfo=TICKER_TZ)
    return local.astimezone(timezone.utc)


def _teams_from_title(title: str) -> tuple[str, str] | None:
    match = TITLE_SPLIT.match(title.strip())
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _ladder_at_ask(orderbook: dict[str, Any], side: str) -> tuple[tuple[Decimal, Decimal], ...] | None:
    """Wave 2: the YES(or NO)-fill ladder as (cost, depth_usd) levels, best first. Taking YES
    consumes resting NO bids, so a NO bid at price p offers YES at cost (1-p); deeper levels
    are worse prices. Used for depth-scaled slippage and depth-capped P&L."""
    book = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or {}
    opposing = book.get("no_dollars" if side == "yes" else "yes_dollars") or []
    levels: list[tuple[Decimal, Decimal]] = []
    for entry in opposing:
        p = Decimal(str(entry[0]))
        size = Decimal(str(entry[1]))
        cost = Decimal("1") - p
        if cost <= 0 or cost >= 1 or size <= 0:
            continue
        levels.append((cost, (size * cost).quantize(Decimal("0.01"))))
    levels.sort(key=lambda level: level[0])  # best (lowest) cost first
    return tuple(levels) or None


def _depth_at_ask(orderbook: dict[str, Any], side: str, ask: Decimal) -> Decimal | None:
    """USD depth at the ask: taking YES consumes resting NO bids (and vice versa).

    The second column's units are not documented; treating it as contracts and
    multiplying by the ask underestimates depth, which only errs toward filtering.
    """
    book = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or {}
    opposing = book.get("no_dollars" if side == "yes" else "yes_dollars") or []
    if not opposing:
        return None
    best = max(opposing, key=lambda level: Decimal(str(level[0])))
    return (Decimal(str(best[1])) * ask).quantize(Decimal("0.01"))


def kalshi_leg_result(leg: dict) -> str | None:
    """Venue-actual resolution for a Kalshi ARB leg (audit fix #1 resolver).

    Reads the settled market's `result` ('yes'/'no'). The leg's YES contract wins iff
    result == 'yes'. Returns None for non-Kalshi legs or markets not yet settled, so
    reconciliation skips them rather than guessing. Polymarket legs need their own
    resolver (conditionId resolution) before mixed-venue arbs fully reconcile.
    """
    if leg.get("venue") != "kalshi":
        return None
    ticker = leg.get("book")
    if not ticker:
        return None
    try:
        resp = requests.get(f"{BASE_URL}/markets/{ticker}",
                            timeout=30, headers={"User-Agent": "arb-paper-scanner/0.1 (public read-only)"})
        if resp.status_code != 200:
            return None
        market = resp.json().get("market", {})
    except requests.RequestException:
        return None
    if str(market.get("status")) not in {"settled", "finalized"}:
        return None
    result = str(market.get("result", "")).lower()
    if result == "yes":
        return "WON"
    if result == "no":
        return "LOST"
    if result in {"void", "voided", "cancelled", "canceled"}:
        return "VOID"
    return None
