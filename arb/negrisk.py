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

import json
from dataclasses import dataclass
from decimal import Decimal

from .pricing import q6

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


def _json_list(value) -> list:
    return json.loads(value) if isinstance(value, str) else list(value)


def find_negrisk_arb(event: dict, fee_rate: Decimal, min_edge: Decimal,
                     require_exhaustive: bool, max_outcomes: int) -> NegRiskArb | None:
    """Detect a convert or overshoot arb in one negative-risk event. None if none."""
    markets = event.get("markets", [])
    if not 2 <= len(markets) <= max_outcomes:
        return None
    exhaustive = bool(event.get("negRisk"))  # venue's own metadata, never self-enumerated
    if require_exhaustive and not exhaustive:
        return None

    asks: list[Decimal] = []
    ask_depths: list[Decimal] = []
    bids: list[Decimal] = []
    for m in markets:
        ask = _dec(m.get("yes_best_ask"))
        bid = _dec(m.get("yes_best_bid"))
        if ask is None or bid is None:
            return None  # an incomplete set cannot be a real basket arb
        asks.append(ask)
        ask_depths.append(_dec(m.get("yes_ask_depth")) or Decimal("0"))
        bids.append(bid)

    # Convert: buy every YES.
    ask_sum = q6(sum(asks, Decimal("0")) + sum((fee_rate * a * (Decimal("1") - a) for a in asks), Decimal("0")))
    if ask_sum < Decimal("1"):
        roi = q6(Decimal("1") / ask_sum - Decimal("1"))
        if roi >= min_edge:
            min_depth = min(ask_depths) if ask_depths else Decimal("0")
            return NegRiskArb(str(event.get("id", "")), str(event.get("title", "")), "convert",
                              len(markets), ask_sum, roi, min_depth, q6(min_depth * roi), exhaustive)

    # Overshoot: sell every YES (proceeds = Σ bids − fees).
    bid_sum = q6(sum(bids, Decimal("0")) - sum((fee_rate * b * (Decimal("1") - b) for b in bids), Decimal("0")))
    if bid_sum > Decimal("1"):
        edge = q6(bid_sum - Decimal("1"))
        if edge >= min_edge:
            return NegRiskArb(str(event.get("id", "")), str(event.get("title", "")), "overshoot",
                              len(markets), bid_sum, edge, Decimal("0"), Decimal("0"), exhaustive)
    return None


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value)).quantize(CENT)
