from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from arb.config import AppConfig
from arb.matching import event_key, load_aliases, normalize_name
from arb.models import Quote
from arb.pricing import all_in_fee_cost, q6
from arb.venues.slippage import effective_buffer

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# SPEC §3.2: Gamma discovery is tag_slug ∈ {mlb|nba|soccer|tennis|mma}.
TAG_SLUGS = {
    "baseball_mlb": "mlb",
    "basketball_nba": "nba",
    "soccer": "soccer",
    "tennis": "tennis",
    "mma": "mma",
}
# v1 scope is winner + totals only (SPEC §1); other sportsMarketTypes are ignored.
MARKET_TYPES = {"moneyline": "WINNER", "totals": "TOTAL"}
TITLE_SPLIT = re.compile(r"^(.+?)\s+(?:vs\.?|at)\s+(.+?)$", flags=re.IGNORECASE)
# CLOB book reads are independent; a small pool keeps total fetch time low without
# hammering the API the way unbounded concurrency would.
BOOK_WORKERS = 6


class PolymarketAdapter:
    name = "polymarket"

    def __init__(self, config: AppConfig, fixture_dir: Path | None = None) -> None:
        self.config = config
        self.fixture_dir = fixture_dir

    def fetch_quotes(self, sport: str) -> list[Quote]:
        if self.fixture_dir is not None:
            data = json.loads((self.fixture_dir / f"polymarket_{sport}.json").read_text())
            return parse_polymarket_events(
                data,
                sport,
                self.config.min_liquidity_usd,
                self.config.polymarket_sports_fee_rate,
                self.config.taker_price_buffer,
            )
        tag_slug = TAG_SLUGS.get(sport)
        if tag_slug is None:
            raise RuntimeError(f"No Polymarket tag_slug mapped for sport {sport!r}; SPEC §3.2 lists mlb/nba/soccer/tennis/mma only.")
        response = requests.get(
            f"{GAMMA_URL}/events",
            params={"tag_slug": tag_slug, "closed": "false", "limit": 100},
            timeout=30,
        )
        response.raise_for_status()
        events = _attach_clob_books(response.json())
        return parse_polymarket_events(
            events,
            sport,
            self.config.min_liquidity_usd,
            self.config.polymarket_sports_fee_rate,
            self.config.taker_price_buffer,
            slippage_model=self.config.taker_slippage_model,
            slippage_ref_stake=self.config.slippage_ref_stake_usd,
        )


def parse_polymarket_events(
    events: list[dict[str, Any]],
    sport: str,
    min_liquidity: Decimal,
    fee_rate: Decimal,
    taker_buffer: Decimal = Decimal("0"),
    slippage_model: str = "flat",
    slippage_ref_stake: Decimal = Decimal("50"),
) -> list[Quote]:
    fetched_at = datetime.now(timezone.utc)
    aliases = load_aliases()
    quotes: list[Quote] = []
    for event in events:
        teams = TITLE_SPLIT.match(str(event.get("title", "")).strip())
        if not teams:
            continue
        home, away = teams.group(1).strip(), teams.group(2).strip()
        for market in event.get("markets", []):
            market_type = MARKET_TYPES.get(str(market.get("sportsMarketType")))
            start_text = market.get("gameStartTime")
            if market_type is None or not start_text:
                continue
            liquidity = Decimal(str(market.get("liquidity") or event.get("liquidity") or "0"))
            if liquidity < min_liquidity:
                continue
            start = _parse_start(str(start_text))
            key = event_key(sport, home, away, start, aliases)
            line = Decimal(str(market["line"])) if market.get("line") is not None else None
            outcomes = _json_list(market.get("outcomes", []))
            prices = _json_list(market.get("clobAskPrices", []))
            depths = _json_list(market.get("clobAskDepths", []))
            token_ids = _json_list(market.get("clobTokenIds", []))
            for index, outcome in enumerate(outcomes):
                if index >= len(prices) or not str(prices[index]):
                    continue
                price = Decimal(str(prices[index]))
                depth = Decimal(str(depths[index])) if index < len(depths) else liquidity
                if market_type == "TOTAL":
                    label = "OVER" if str(outcome).upper().startswith("O") else "UNDER"
                else:
                    label = normalize_name(str(outcome), sport, aliases)
                # Batch markets endpoint exposes top-of-book only (one ask price+depth); a
                # multi-level ladder would need per-token /book fetches. 1-level ladder here
                # means slippage stays flat for Polymarket until book-fetching is added.
                ladder = ((price, depth),) if depth and depth > 0 else None
                buffer = effective_buffer(ladder, slippage_model, slippage_ref_stake, taker_buffer)
                quotes.append(
                    Quote(
                        event_key=key,
                        venue="polymarket",
                        book=str(token_ids[index] if index < len(token_ids) else market.get("id", "unknown")),
                        market_type=market_type,
                        line=line,
                        outcome_label=label,
                        raw_price=price,
                        cost=price,
                        fee_cost=all_in_fee_cost(price, fee_rate, buffer, basis="linear"),
                        depth_usd=depth,
                        ladder=ladder,
                        live=False,
                        fetched_at=fetched_at,
                        start_utc=start,
                        home=normalize_name(home, sport, aliases),
                        away=normalize_name(away, sport, aliases),
                    )
                )
    return quotes


def _parse_start(text: str) -> datetime:
    # Gamma emits "2026-06-10 23:40:00+00"; fromisoformat (3.11+) accepts it.
    start = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


def _fetch_book(token_id: Any) -> tuple[str, str]:
    response = requests.get(f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=30)
    if response.status_code == 404:  # token has no book yet — not a quote
        return "", "0"
    response.raise_for_status()
    best = _best_ask(response.json().get("asks", []))
    if best is None:
        return "", "0"
    price, depth = best
    return str(price), str(depth)


def _attach_clob_books(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # SPEC §3.2: executable prices come from the CLOB book only; Gamma outcomePrices are stale mids.
    eligible = [
        market
        for event in events
        for market in event.get("markets", [])
        if str(market.get("sportsMarketType")) in MARKET_TYPES and market.get("gameStartTime")
    ]
    token_lists = [_json_list(market.get("clobTokenIds", [])) for market in eligible]
    flat_tokens = [token for tokens in token_lists for token in tokens]
    with ThreadPoolExecutor(max_workers=BOOK_WORKERS) as pool:
        books = list(pool.map(_fetch_book, flat_tokens))
    cursor = 0
    for market, tokens in zip(eligible, token_lists):
        results = books[cursor:cursor + len(tokens)]
        cursor += len(tokens)
        # Lists stay index-aligned with `outcomes`; empty string marks an empty book.
        market["clobAskPrices"] = [price for price, _ in results]
        market["clobAskDepths"] = [depth for _, depth in results]
    return events


def _best_ask(levels: list[dict[str, Any]]) -> tuple[Decimal, Decimal] | None:
    parsed = [(Decimal(str(level["price"])), Decimal(str(level["size"]))) for level in levels]
    parsed = [(price, size) for price, size in parsed if price > 0 and size > 0]
    if not parsed:
        return None
    price, size = min(parsed, key=lambda level: level[0])
    return price, q6(price * size)
