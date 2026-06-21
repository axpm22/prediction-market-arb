from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from arb.config import AppConfig
from arb.matching import event_key, load_aliases, normalize_name
from arb.models import Quote
from arb.pricing import q6

BASE_URL = "https://api.sx.bet"
PCT_SCALE = Decimal("100000000000000000000")
USDC_SCALE = Decimal("1000000")
# SPEC §3.3, confirmed live via GET /sports June 2026.
SPORT_IDS = {"basketball_nba": 1, "baseball_mlb": 3, "soccer": 5, "tennis": 6, "mma": 7}
# SX sport ids cover whole sports (Baseball includes NPB/KBO); restrict league-scoped
# sport keys to their league so foreign games don't enter the event space.
LEAGUE_FILTER = {"baseball_mlb": {"MLB"}, "basketball_nba": {"NBA"}}
MONEYLINE, TOTALS = 226, 28
ORDER_BATCH = 20
# api.sx.bet rejects default python user agents with 403.
USER_AGENT = "arb-paper-scanner/0.1 (public read-only)"


class SxAdapter:
    name = "sx"

    def __init__(self, config: AppConfig, fixture_dir: Path | None = None) -> None:
        self.config = config
        self.fixture_dir = fixture_dir
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def fetch_quotes(self, sport: str) -> list[Quote]:
        if self.fixture_dir is not None:
            data = json.loads((self.fixture_dir / f"sx_{sport}.json").read_text())
            return parse_sx_payload(data, sport, fee_rate=self.config.sx_fee_rate)
        if self.config.sx_fee_rate is None:
            raise RuntimeError("SX adapter refuses live runs while sx.fee_rate is null.")
        sport_id = SPORT_IDS.get(sport)
        if sport_id is None:
            return []
        allowed_labels = LEAGUE_FILTER.get(sport)
        markets: list[dict[str, Any]] = []
        for league in self._get(f"/leagues/active?sportId={sport_id}"):
            if allowed_labels is not None and str(league.get("label")) not in allowed_labels:
                continue
            markets.extend(self._fetch_league_markets(int(league["leagueId"])))
        markets = [m for m in markets if int(m.get("type", 0)) in {MONEYLINE, TOTALS}]
        orders = self._fetch_orders([str(m["marketHash"]) for m in markets])
        return parse_sx_payload({"markets": markets, "orders": orders}, sport, fee_rate=self.config.sx_fee_rate)

    def _fetch_league_markets(self, league_id: int) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        pagination_key = ""
        while True:
            path = f"/markets/active?leagueId={league_id}&onlyMainLine=true"
            if pagination_key:
                path += f"&paginationKey={pagination_key}"
            data = self._get(path)
            markets.extend(data.get("markets", []))
            pagination_key = data.get("nextKey") or ""
            if not pagination_key:
                return markets

    def _fetch_orders(self, market_hashes: list[str]) -> dict[str, list[dict[str, Any]]]:
        by_hash: dict[str, list[dict[str, Any]]] = {}
        for start in range(0, len(market_hashes), ORDER_BATCH):
            batch = market_hashes[start:start + ORDER_BATCH]
            for order in self._get(f"/orders?marketHashes={','.join(batch)}"):
                by_hash.setdefault(str(order["marketHash"]), []).append(order)
        return by_hash

    def _get(self, path: str) -> Any:
        response = self.session.get(f"{BASE_URL}{path}", timeout=30)
        response.raise_for_status()
        return response.json()["data"]


def taker_cost_from_percentage_odds(percentage_odds: str) -> Decimal:
    maker_probability = Decimal(percentage_odds) / PCT_SCALE
    return q6(Decimal("1") - maker_probability)


def parse_sx_payload(payload: dict[str, Any], sport: str, fee_rate: Decimal | None) -> list[Quote]:
    fetched_at = datetime.now(timezone.utc)
    aliases = load_aliases()
    # SPEC §3.3: SX void rules differ from the other venues; legs stay flagged for REVIEW.
    notes: tuple[str, ...] = ("settlement-rule-review",)
    if fee_rate is None:
        fee_rate = Decimal("0")
        notes = (*notes, "fee-rate-unverified")
    orders_by_hash = payload.get("orders", {})
    quotes: list[Quote] = []
    for market in payload.get("markets", []):
        market_type_code = int(market.get("type", 0))
        if market_type_code not in {MONEYLINE, TOTALS}:
            continue
        market_hash = str(market["marketHash"])
        start = datetime.fromtimestamp(int(market["gameTime"]), tz=timezone.utc)
        home = str(market["teamOneName"])
        away = str(market["teamTwoName"])
        key = event_key(sport, home, away, start, aliases)
        market_type = "WINNER" if market_type_code == MONEYLINE else "TOTAL"
        line = Decimal(str(market["line"])) if market.get("line") is not None else None
        for index, maker_side in [(1, False), (2, True)]:
            best = _best_taker_order(orders_by_hash.get(market_hash, []), maker_side)
            if best is None:
                continue
            cost, depth = best
            raw_label = str(market.get(f"outcome{'One' if index == 1 else 'Two'}Name") or (home if index == 1 else away))
            if market_type == "TOTAL":
                label = "OVER" if raw_label.upper().startswith("OVER") else "UNDER"
            else:
                label = normalize_name(raw_label, sport, aliases)
            quotes.append(
                Quote(
                    event_key=key,
                    venue="sx",
                    book=market_hash,
                    market_type=market_type,
                    line=line,
                    outcome_label=label,
                    raw_price=cost,
                    cost=cost,
                    fee_cost=q6(fee_rate * cost * (Decimal("1") - cost)),
                    depth_usd=depth,
                    live=False,
                    fetched_at=fetched_at,
                    start_utc=start,
                    home=normalize_name(home, sport, aliases),
                    away=normalize_name(away, sport, aliases),
                    notes=notes,
                    ref=str(index),  # outcome side ("1"/"2") so this leg can be re-quoted
                )
            )
    return quotes


def requote_sx_leg(market_hash: str, ref: str | None, fee_rate: Decimal | None,
                   session: requests.Session | None = None) -> Decimal | None:
    """Re-price one SX leg now: fetch live orders for its market, pick the best taker
    order on the leg's side, return effective cost (taker cost + fee). None if the book
    is empty or the leg lacks the side hint. This is what makes SX verifiable."""
    if ref not in {"1", "2"}:
        return None
    maker_side = ref == "2"  # outcome two's taker reads maker-betting-outcome-one orders
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", USER_AGENT)
    response = sess.get(f"{BASE_URL}/orders?marketHashes={market_hash}", timeout=30)
    response.raise_for_status()
    orders = response.json().get("data", [])
    best = _best_taker_order(orders, maker_side)
    if best is None:
        return None
    cost, _depth = best
    rate = fee_rate if fee_rate is not None else Decimal("0")
    return q6(cost + rate * cost * (Decimal("1") - cost))


def _best_taker_order(orders: list[dict[str, Any]], maker_betting_outcome_one: bool) -> tuple[Decimal, Decimal] | None:
    candidates = [
        order for order in orders
        if bool(order.get("isMakerBettingOutcomeOne")) is maker_betting_outcome_one
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda order: Decimal(str(order["percentageOdds"])))
    cost = taker_cost_from_percentage_odds(str(best["percentageOdds"]))
    remaining = Decimal(str(best.get("totalBetSize") or "0")) - Decimal(str(best.get("fillAmount") or "0"))
    return cost, remaining / USDC_SCALE
