"""SX Bet adapter (SPEC §6.3). RULE 10: percentageOdds is the MAKER's implied
probability x 1e20 — the taker takes the opposite side:
  taker cost of outcome i = 1 - percentageOdds/1e20 over orders where the maker
  bets the OTHER outcome; best price = max percentageOdds.
  taker depth at a level  = maker_stake / p CONTRACTS (x cost = USD); ladders
  store CONTRACTS like every other venue (sizing walks contract counts).
SX legs start REVIEW until whitelisted per sport; settled-status semantics
unverified (data/verified_facts.md) so they cannot leave REVIEW yet."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

from arb.logutil import get_logger
from arb.models import Quote
from arb.venues.base import Deadline, PartialFetch, get_json, is_live

log = get_logger("arb.sx", "logs/scan.log")

BASE = "https://api.sx.bet"
SPORT_IDS = {"basketball_nba": 1, "baseball_mlb": 3, "soccer": 5, "tennis": 6, "mma": 7}

# An SX sportId is a BROAD CATEGORY, not a canonical sport. Verified live
# 2026-07-29: sportId 1 (Basketball) had exactly one active league — WNBA — so
# every SX basketball quote was filed as basketball_nba and could never match
# the Kalshi/PM WNBA legs. sportId 3 (Baseball) carries MLB, NPB and KBO
# (fixture sx_leagues.json), all previously labelled baseball_mlb.
# Keyed on the league `label`; an unlisted league keeps the sportId's default,
# so a new league is mislabelled-as-before rather than dropped.
LEAGUE_SPORT = {"wnba": "basketball_wnba", "nba": "basketball_nba",
                "mlb": "baseball_mlb", "npb": "baseball_npb",
                "kbo league": "baseball_kbo"}


def league_sport(label: str, default: str) -> str:
    """Canonical sport for one SX league. See LEAGUE_SPORT."""
    return LEAGUE_SPORT.get((label or "").strip().lower(), default)


def sport_ids_to_fetch(cfg) -> list[int]:
    """Distinct sportIds covering the configured sports. basketball_nba and
    basketball_wnba share sportId 1 — iterating cfg['sports'] would fetch it
    twice and double every quote."""
    seen, out = set(), []
    for sport in cfg["sports"]:
        sid = SPORT_IDS.get(sport)
        if sid is not None and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out
TYPE_MONEYLINE, TYPE_TOTALS = 226, 28  # 342 spread: ignore
E20 = Decimal(10) ** 20
E6 = Decimal(10) ** 6


def _levels(orders: list[dict], maker_betting_one: bool) -> list[tuple[Decimal, Decimal]]:
    """(taker_cost, taker_depth_CONTRACTS) levels for the outcome the MAKER is
    not betting, cheapest first. Contracts, not USD: the shared ladder consumer
    (sizing.vwap_cost/q_star) walks contract counts (review 2026-07-10; sx
    ladders persisted before that date carry USD in this slot)."""
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
        levels.append((1 - p, by_odds[p] / p))  # contracts = maker stake / p
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
        cost, contracts = levels[0]
        if mtype == "TOTAL":
            label = "OVER" if "OVER" in outcome_name.upper() else "UNDER"
        else:
            label = outcome_name
        quotes.append(Quote(
            event_key="", venue="sx", book="sx", market_type=mtype, line=line,
            outcome_label=label, side="YES", venue_side="YES", raw_price=cost, cost=cost,
            fee_cost=Decimal(0),  # singles 0% (§6.5); parlay 5% lives in its lane
            depth_usd=contracts * cost,  # USD, unchanged basis
            ladder_json=str([[str(c), str(d)] for c, d in levels]),
            live=is_live(start, now),
            fetched_at=now, category="sports", sport=sport,
            bid=None, volume_24h=None,  # SX prints no volume field; zombie guard n/a
            home=market.get("teamTwoName", ""), away=market.get("teamOneName", ""),
            start_utc=start, start_grade="nominal" if start else "absent",
            venue_market_id=market["marketHash"]))
    return quotes


def _fetch_sport(cfg, sport_id: int, deadline: Deadline) -> list[Quote]:
    """One sportId's leagues -> paginated markets -> chunked orders. Checks the
    per-venue deadline between every paginated/chunk request (H7). Each LEAGUE
    carries its own canonical sport (see LEAGUE_SPORT) — the sportId alone is
    too coarse — and leagues outside cfg['sports'] are skipped."""
    quotes: list[Quote] = []
    if deadline.expired():
        return quotes
    default = next((s for s, i in SPORT_IDS.items() if i == sport_id), None)
    if default is None:
        return quotes
    wanted = set(cfg["sports"])
    leagues = get_json(f"{BASE}/leagues/active?sportId={sport_id}", cfg,
                       deadline=deadline).get("data", [])
    for lg in leagues:
        if deadline.expired():
            return quotes
        sport = league_sport(lg.get("label", ""), default)
        if sport not in wanted:
            continue
        markets, next_key = [], ""
        for _ in range(10):
            if deadline.expired():
                return quotes
            url = f"{BASE}/markets/active?leagueId={lg['leagueId']}&onlyMainLine=true"
            if next_key:
                url += f"&paginationKey={next_key}"
            data = get_json(url, cfg, deadline=deadline).get("data", {})
            markets.extend(data.get("markets", []))
            next_key = data.get("nextKey") or ""
            if not next_key:
                break
        else:
            if next_key:  # cap hit with pages pending — never truncate silently
                log.warning("sx pagination cap hit for league %s; coverage truncated",
                            lg["leagueId"])
        for i in range(0, len(markets), 20):
            if deadline.expired():
                return quotes
            chunk = markets[i:i + 20]
            hashes = ",".join(m["marketHash"] for m in chunk)
            orders = get_json(f"{BASE}/orders?marketHashes={hashes}", cfg,
                              deadline=deadline).get("data", [])
            for m in chunk:
                quotes.extend(quotes_from_orders(cfg, m, orders, sport))
    return quotes


def fetch_markets(cfg) -> list[Quote]:
    # per-sport fan-out in parallel (H7): venue time ~ slowest single sport, not
    # the serial sum. Thread-local Sessions (base.py) keep this connection-safe.
    deadline = Deadline(cfg["execution"]["venue_deadline_secs"])
    sports = sport_ids_to_fetch(cfg)   # distinct sportIds, never one per sport
    with ThreadPoolExecutor(max_workers=max(1, len(sports))) as pool:
        per_sport = list(pool.map(lambda s: _fetch_sport(cfg, s, deadline), sports))
    quotes = [q for sub in per_sport for q in sub]
    if deadline.expired():  # truncated -> partial, never a full-coverage poll
        raise PartialFetch(quotes)
    return quotes
