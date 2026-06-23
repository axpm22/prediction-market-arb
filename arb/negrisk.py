"""Negative-risk basket arbitrage — BUILD_PLAN S1 (Grade A, build first).

Within ONE Polymarket negative-risk event, the outcomes are mutually exclusive and
exhaustive and all settle off the SAME resolution. The law of one price says the YES
prices of the full set sum to $1. There is no cross-venue matching and no settlement
mismatch — every defense layer the cross-venue arbs need, this class doesn't.

- Convert arb:  Σ best_ask(YES_i) + fees < 1  → buy one YES of every outcome → locked $1.
- Overshoot arb: Σ best_bid(YES_i) − fees > 1  → sell the overpriced basket.

Depth-aware: the basket is capped to the MINIMUM executable depth across legs (one thin
leg caps the whole basket). Gated on the venue's own exhaustive+exclusive metadata —
never on our own enumeration (an incomplete set fakes an arb).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .pricing import all_in_fee_cost, q6

CENT = Decimal("0.000001")


@dataclass(frozen=True)
class NegRiskArb:
    event_id: str
    title: str
    kind: str            # 'convert' | 'overshoot'
    basket_n: int
    basket_sum: Decimal  # Σ asks (convert) or Σ bids (overshoot)
    edge: Decimal        # locked ROI (convert) or proceeds-1 (overshoot)
    min_depth_usd: Decimal
    capturable_usd: Decimal
    exhaustive_exclusive: bool
    legs: list[dict[str, str]]
    quality_flag: str | None = None


def find_negrisk_arb(
    event: dict,
    fee_rate: Decimal,
    min_edge: Decimal,
    require_exhaustive: bool,
    max_outcomes: int,
    min_executable_usd: Decimal | None = None,
    taker_buffer: Decimal = Decimal("0"),
) -> NegRiskArb | None:
    """Detect a convert or overshoot arb in one negative-risk event. None if none."""
    markets = event.get("markets", [])
    if not 2 <= len(markets) <= max_outcomes:
        return None
    exhaustive = bool(event.get("negRisk"))  # venue's own metadata, never self-enumerated
    if require_exhaustive and not exhaustive:
        return None

    asks: list[Decimal] = []
    ask_depths: list[Decimal] = []
    ask_legs: list[dict[str, str]] = []
    bids: list[Decimal] = []
    bid_depths: list[Decimal] = []
    bid_legs: list[dict[str, str]] = []
    for index, m in enumerate(markets):
        ask = _dec(m.get("yes_best_ask"))
        bid = _dec(m.get("yes_best_bid"))
        if ask is None or bid is None:
            return None  # an incomplete set cannot be a real basket arb
        # C0: these are Polymarket markets, so fees follow the LINEAR model (rate·price), not
        # the Kalshi quadratic rate·p·(1−p). The quadratic curve under-charges (1−p < 1), which
        # shrinks Σasks+fees below 1 and manufactures fake convert arbs in the safest class.
        ask_fee = all_in_fee_cost(ask, fee_rate, taker_buffer, basis="linear")
        bid_fee = all_in_fee_cost(bid, fee_rate, taker_buffer, basis="linear")
        ask_depth = _dec(m.get("yes_ask_depth")) or Decimal("0")
        bid_depth = _dec(m.get("yes_bid_depth")) or Decimal("0")
        asks.append(q6(ask + ask_fee))
        ask_depths.append(ask_depth)
        ask_legs.append(_leg_json(m, index, "BUY_YES", ask, ask_fee, q6(ask + ask_fee), ask_depth))
        bids.append(q6(bid - bid_fee))
        bid_depths.append(bid_depth)
        bid_legs.append(_leg_json(m, index, "SELL_YES", bid, bid_fee, q6(bid - bid_fee), bid_depth))

    # Convert: buy every YES.
    ask_sum = q6(sum(asks, Decimal("0")))
    if ask_sum < Decimal("1"):
        roi = q6(Decimal("1") / ask_sum - Decimal("1"))
        if roi >= min_edge:
            min_depth = min(ask_depths) if ask_depths else Decimal("0")
            quality_flag = _quality_flag(min_depth, min_executable_usd)
            return NegRiskArb(str(event.get("id", "")), str(event.get("title", "")), "convert",
                              len(markets), ask_sum, roi, min_depth, q6(min_depth * roi), exhaustive,
                              ask_legs, quality_flag)

    # Overshoot: sell every YES (proceeds = Σ bids − fees).
    bid_sum = q6(sum(bids, Decimal("0")))
    if bid_sum > Decimal("1"):
        edge = q6(bid_sum - Decimal("1"))
        if edge >= min_edge:
            min_depth = min(bid_depths) if bid_depths else Decimal("0")
            quality_flag = _quality_flag(min_depth, min_executable_usd)
            return NegRiskArb(str(event.get("id", "")), str(event.get("title", "")), "overshoot",
                              len(markets), bid_sum, edge, min_depth, q6(min_depth * edge), exhaustive,
                              bid_legs, quality_flag)
    return None


def _leg_json(market: dict, index: int, side: str, price: Decimal, fee: Decimal,
              effective_price: Decimal, depth_usd: Decimal) -> dict[str, str]:
    return {
        "market_id": str(market.get("id") or market.get("conditionId") or index),
        "outcome": str(market.get("outcome") or market.get("question") or f"outcome-{index + 1}"),
        "side": side,
        "price": str(price),
        "fee": str(fee),
        "effective_price": str(effective_price),
        "depth_usd": str(depth_usd),
    }


def _quality_flag(min_depth: Decimal, min_executable_usd: Decimal | None) -> str | None:
    if min_executable_usd is not None and min_depth < min_executable_usd:
        return "thin-leg"
    return None


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value)).quantize(CENT)
