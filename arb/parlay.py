"""Parlay opportunity class (SPEC §11) — pipeline integration.

A parlay pays $1 iff ALL legs win. There is no static replication, so a parlay can never be
locked risk-free like a TRUE ARB — these are EV-style signals with real outcome variance.
Hard rule: every parlay opportunity is `status=REVIEW` and is NEVER auto-filled. The flat-stake
tracker records it in its own ledger lane (kind='PARLAY'); settlement resolves it as the AND of
its component legs.

Two model-free signals (math lives in `experimental/parlay.py`, reused here):
- Fréchet-underpriced: ask below max(0, Σpᵢ−(n−1)) — cheap under ANY correlation. The only
  signal valid for same-game legs (positive correlation makes the independence product invalid).
- independence-cheap: cross-game legs only, ask below Πpᵢ by ≥ threshold.

`ParlayMarket` / `ParlayLeg` are the venue-agnostic input contract. Venue adapters (Kalshi MVE,
Polymarket CAOC, SX Daily Parlays — SPEC §11) map their payloads onto it; the field-mapping for
each live endpoint must be verified before live use (hard rule #3), so this module is exercised
fixtures-first via `load_parlay_markets`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from .experimental.parlay import classify_parlay_ask, classify_parlay_bid, frechet_bounds, independence_fair
from .models import Event, Opportunity, Quote
from .pricing import q6

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class ParlayLeg:
    """One component of a parlay, priced from the SAME venue's individual single-leg market.
    `prob` is that single leg's de-vigged implied win probability (the Fréchet/independence
    input). The rest is what settlement needs to resolve the leg against scores."""
    event_key: str
    sport: str
    market_type: str            # "WINNER" | "TOTAL"
    outcome_label: str
    prob: Decimal
    line: Decimal | None = None
    start_utc: datetime | None = None
    home: str | None = None
    away: str | None = None


@dataclass(frozen=True)
class ParlayMarket:
    """A tradeable parlay instrument: one ask/bid for $1 if every leg wins."""
    venue: str
    market_id: str
    ask: Decimal                # cost per $1 payout (= implied prob of the basket)
    legs: list[ParlayLeg]
    same_game: bool
    bid: Decimal | None = None
    fee_rate: Decimal = ZERO    # winning-bet fee rate (SX parlays charge on winnings)
    depth_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    category: str = "sports"
    notes: tuple[str, ...] = field(default_factory=tuple)


def _win_fee_cost(price: Decimal, fee_rate: Decimal) -> Decimal:
    """Conservative winning-bet fee on a $1-payout contract bought at `price` (mirrors the SX
    win-fee model, D3): the parlay's one payout incurs fee_rate of the winnings (1−price)."""
    if fee_rate <= 0 or price <= 0 or price >= 1:
        return ZERO
    return q6(fee_rate * (ONE - price))


def detect_parlays(
    markets: list[ParlayMarket],
    divergence_threshold: Decimal,
    min_liquidity_usd: Decimal = ZERO,
    now: datetime | None = None,
) -> list[Opportunity]:
    """Classify each parlay market and emit a REVIEW-only PARLAY Opportunity per signal.

    Liquidity is enforced strictly (SPEC §11 observed zero-liquidity Kalshi MVE books). The
    basket ask is charged the winning-bet fee before comparison so a fee-eroded edge drops out.
    """
    now = now or datetime.now(timezone.utc)
    opportunities: list[Opportunity] = []
    for market in markets:
        if not market.legs:
            continue
        leg_probs = [leg.prob for leg in market.legs]
        ask_fee = _win_fee_cost(market.ask, market.fee_rate)
        eff_ask = q6(market.ask + ask_fee)
        lower, upper = frechet_bounds(leg_probs)
        fair = independence_fair(leg_probs)

        signal = classify_parlay_ask(eff_ask, leg_probs, divergence_threshold, market.same_game)
        side = "BUY"
        if signal is not None:
            bound = lower if signal == "frechet-underpriced" else fair
            edge_points = q6(bound - eff_ask)
        else:
            bid_signal = classify_parlay_bid(market.bid, leg_probs) if market.bid is not None else None
            if bid_signal is None:
                continue
            signal, side = bid_signal, "SELL"
            edge_points = q6(market.bid - upper)

        notes = [
            f"parlay-{signal}",
            f"side:{side}",
            f"ask:{market.ask}",
            f"eff_ask:{eff_ask}",
            f"frechet:[{lower},{upper}]",
            f"fair:{fair}",
            f"same_game:{market.same_game}",
            "review-never-auto-filled",
            *market.notes,
        ]

        liquidity = market.liquidity_usd
        if min_liquidity_usd > 0 and (liquidity is None or liquidity < min_liquidity_usd):
            notes.append(f"thin-parlay-book:{liquidity}")

        basket = _basket_quote(market, eff_ask, ask_fee, now)
        components = [_component_quote(market, leg, index, now) for index, leg in enumerate(market.legs)]
        event = _parlay_event(market)
        opportunities.append(
            Opportunity(
                kind="PARLAY",
                category=market.category,  # type: ignore[arg-type]
                event=event,
                legs=[basket, *components],
                edge=edge_points,
                edge_points=edge_points,
                stakes={"PARLAY": ONE},
                status="REVIEW",
                notes=tuple(notes),
                quality_flag="thin-parlay-book" if (min_liquidity_usd > 0 and (liquidity is None or liquidity < min_liquidity_usd)) else None,
            )
        )
    return opportunities


def _basket_quote(market: ParlayMarket, eff_ask: Decimal, ask_fee: Decimal, now: datetime) -> Quote:
    starts = [leg.start_utc for leg in market.legs if leg.start_utc is not None]
    start = min(starts) if starts else None
    return Quote(
        event_key=_parlay_event_key(market),
        venue=market.venue,  # type: ignore[arg-type]
        book=market.market_id,
        market_type="PARLAY",
        line=None,
        outcome_label="PARLAY",
        raw_price=market.ask,
        cost=market.ask,
        fee_cost=ask_fee,
        depth_usd=market.depth_usd,
        live=False,
        fetched_at=now,
        start_utc=start,
    )


def _component_quote(market: ParlayMarket, leg: ParlayLeg, index: int, now: datetime) -> Quote:
    return Quote(
        event_key=leg.event_key,
        venue=market.venue,  # type: ignore[arg-type]
        book=f"{market.market_id}:leg{index}",
        market_type=leg.market_type,  # type: ignore[arg-type]
        line=leg.line,
        outcome_label=leg.outcome_label,
        raw_price=leg.prob,
        cost=leg.prob,
        fee_cost=ZERO,
        depth_usd=None,
        live=False,
        fetched_at=now,
        start_utc=leg.start_utc,
        home=leg.home,
        away=leg.away,
    )


def _parlay_event_key(market: ParlayMarket) -> str:
    legs = "+".join(sorted(leg.event_key for leg in market.legs))
    return f"parlay:{market.venue}:{market.market_id}:{legs}"


def _parlay_event(market: ParlayMarket) -> Event:
    starts = [leg.start_utc for leg in market.legs if leg.start_utc is not None]
    start = min(starts) if starts else datetime.now(timezone.utc)
    return Event(
        event_key=_parlay_event_key(market),
        category=market.category,  # type: ignore[arg-type]
        sport=market.legs[0].sport,
        home="",
        away="",
        start_utc=start,
    )


def load_parlay_markets(raw: list[dict]) -> list[ParlayMarket]:
    """Map the venue-agnostic fixture/JSON shape onto ParlayMarket. Documented schema:

        {"venue","market_id","ask","bid"?,"fee_rate"?,"depth_usd"?,"liquidity_usd"?,
         "same_game","category"?, "legs":[{"event_key","sport","market_type","outcome_label",
         "prob","line"?,"start_utc"?,"home"?,"away"?}, ...]}
    """
    markets: list[ParlayMarket] = []
    for item in raw:
        legs = [
            ParlayLeg(
                event_key=str(leg["event_key"]),
                sport=str(leg["sport"]),
                market_type=str(leg["market_type"]),
                outcome_label=str(leg["outcome_label"]),
                prob=Decimal(str(leg["prob"])),
                line=None if leg.get("line") in (None, "") else Decimal(str(leg["line"])),
                start_utc=_parse_dt(leg.get("start_utc")),
                home=leg.get("home"),
                away=leg.get("away"),
            )
            for leg in item["legs"]
        ]
        markets.append(
            ParlayMarket(
                venue=str(item["venue"]),
                market_id=str(item["market_id"]),
                ask=Decimal(str(item["ask"])),
                legs=legs,
                same_game=bool(item["same_game"]),
                bid=None if item.get("bid") in (None, "") else Decimal(str(item["bid"])),
                fee_rate=Decimal(str(item.get("fee_rate", "0"))),
                depth_usd=None if item.get("depth_usd") in (None, "") else Decimal(str(item["depth_usd"])),
                liquidity_usd=None if item.get("liquidity_usd") in (None, "") else Decimal(str(item["liquidity_usd"])),
                category=str(item.get("category", "sports")),
                notes=tuple(item.get("notes", ())),
            )
        )
    return markets


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
