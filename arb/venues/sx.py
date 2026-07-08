"""SX Bet adapter (SPEC §6.3). RULE 10: percentageOdds is the MAKER's implied
probability x 1e20 — the taker takes the opposite side:
  taker cost of outcome i = 1 - percentageOdds/1e20 over orders where the maker
  bets the OTHER outcome; best price = max percentageOdds.
  taker depth at a level  = maker_stake * c / (1 - c).
SX legs start REVIEW until whitelisted per sport; settled-status semantics
unverified (data/verified_facts.md) so they cannot leave REVIEW yet."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from arb.models import Quote
from arb.venues.base import get_json

BASE = "https://api.sx.bet"
SPORT_IDS = {"basketball_nba": 1, "baseball_mlb": 3, "soccer": 5, "tennis": 6, "mma": 7}
TYPE_MONEYLINE, TYPE_TOTALS = 226, 28  # 342 spread: ignore
E20 = Decimal(10) ** 20
E6 = Decimal(10) ** 6


def _levels(orders: list[dict], maker_betting_one: bool) -> list[tuple[Decimal, Decimal]]:
    """(taker_cost, taker_depth_usd) levels for the outcome the MAKER is not betting,
    cheapest first."""
    by_odds: dict[Decimal, Decimal] = {}
    for o in orders:
        if o["isMakerBettingOutcomeOne"] != maker_betting_one:
            continue
        p = Decimal(o["percentageOdds"]) / E20
        stake = (Decimal(o["totalBetSize"]) - Decimal(o["fillAmount"])) / E6
        if stake > 0:
            by_odds[p] = by_odds.get(p, Decimal(0)) + stake
    levels = []
    for p in sorted(by_odds, reverse=True):  # highest maker odds = cheapest taker cost
        c = 1 - p
        levels.append((c, by_odds[p] * c / (1 - c)))
    return levels


def quotes_from_orders(cfg, market: dict, orders: list[dict], sport: str) -> list[Quote]:
    quotes: list[Quote] = []
    now = datetime.now(timezone.utc)
    mtype = "WINNER" if market["type"] == TYPE_MONEYLINE else \
            "TOTAL" if market["type"] == TYPE_TOTALS else None
    if mtype is None:
        return []
    start = datetime.fromtimestamp(market["gameTime"], tz=timezone.utc) \
        if market.get("gameTime") else None
    line = str(Decimal(str(market["line"]))) if market.get("line") is not None else None
    mine = [o for o in orders if o["marketHash"] == market["marketHash"]]
    for outcome_name, maker_betting_one in (
            (market["outcomeOneName"], False),   # taker buys outcome one from makers on two
            (market["outcomeTwoName"], True)):
        levels = _levels(mine, maker_betting_one)
        if not levels:
            continue
        cost, depth = levels[0]
        if mtype == "TOTAL":
            label = "OVER" if "OVER" in outcome_name.upper() else "UNDER"
        else:
            label = outcome_name
        quotes.append(Quote(
            event_key="", venue="sx", book="sx", market_type=mtype, line=line,
            outcome_label=label, side="YES", venue_side="YES", raw_price=cost, cost=cost,
            fee_cost=Decimal(0),  # singles 0% (§6.5); parlay 5% lives in its lane
            depth_usd=depth,
            ladder_json=str([[str(c), str(d)] for c, d in levels]),
            live=start is not None and start < now,
            fetched_at=now, category="sports", sport=sport,
            bid=None, volume_24h=None,  # SX prints no volume field; zombie guard n/a
            home=market.get("teamTwoName", ""), away=market.get("teamOneName", ""),
            start_utc=start, start_grade="nominal" if start else "absent",
            venue_market_id=market["marketHash"]))
    return quotes


def fetch_markets(cfg) -> list[Quote]:
    quotes = []
    for sport in cfg["sports"]:
        sport_id = SPORT_IDS.get(sport)
        if sport_id is None:
            continue
        leagues = get_json(f"{BASE}/leagues/active?sportId={sport_id}", cfg).get("data", [])
        for lg in leagues:
            markets, next_key = [], ""
            for _ in range(10):
                url = f"{BASE}/markets/active?leagueId={lg['leagueId']}&onlyMainLine=true"
                if next_key:
                    url += f"&paginationKey={next_key}"
                data = get_json(url, cfg).get("data", {})
                markets.extend(data.get("markets", []))
                next_key = data.get("nextKey") or ""
                if not next_key:
                    break
            for i in range(0, len(markets), 20):
                chunk = markets[i:i + 20]
                hashes = ",".join(m["marketHash"] for m in chunk)
                orders = get_json(f"{BASE}/orders?marketHashes={hashes}", cfg).get("data", [])
                for m in chunk:
                    quotes.extend(quotes_from_orders(cfg, m, orders, sport))
    return quotes


def fetch_market_status(cfg, market_hashes: list[str]) -> list[dict]:
    """GET /markets/find — settled-status semantics UNVERIFIED (verified_facts.md);
    reconcile treats anything but a verified settled status as None."""
    hashes = ",".join(market_hashes)
    return get_json(f"{BASE}/markets/find?marketHashes={hashes}", cfg).get("data", [])
