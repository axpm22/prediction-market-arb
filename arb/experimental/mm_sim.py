"""Market-making / spread-capture simulator — BUILD_PLAN S6 (Grade C, SIMULATION ONLY).

Polymarket pays daily maker rebates and liquidity rewards for resting limit orders near
the midpoint (rewards accrue even if unfilled); makers pay zero trading fees. The closest
thing to passive yield in the space — BUT it requires placing live limit orders, which
violates hard rule #1. So this builds the ESTIMATOR, not a live quoter. Going live is a
separate decision that means lifting hard rule #1; this module never places an order.

P&L = (spread captured on filled size) + (liquidity reward for resting size/tightness)
      − (adverse-selection cost when the market runs through your quote).
The adverse-selection term is the one that kills naive MM — it is modeled explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class MMScenario:
    size_usd: Decimal
    inside_spread_cents: int
    fill_prob: Decimal           # estimated from observed subsequent trades/quote moves
    spread_cents: int            # full market spread
    reward_rate_daily: Decimal   # liquidity reward as a fraction of resting size per day
    adverse_run_prob: Decimal    # P(market runs through the quote)
    adverse_loss_cents: int      # loss in cents when it runs through


def simulate(s: MMScenario) -> dict:
    """Expected daily P&L for one (size, tightness) maker scenario. Labelled a simulation."""
    cent = Decimal("0.01")
    captured_spread = s.fill_prob * s.size_usd * (Decimal(s.spread_cents - s.inside_spread_cents) * cent)
    reward = s.reward_rate_daily * s.size_usd                       # accrues even if unfilled
    adverse = s.fill_prob * s.adverse_run_prob * s.size_usd * (Decimal(s.adverse_loss_cents) * cent)
    net = captured_spread + reward - adverse
    return {
        "size_usd": str(s.size_usd),
        "inside_spread_cents": s.inside_spread_cents,
        "captured_spread": _m(captured_spread),
        "reward": _m(reward),
        "adverse_selection": _m(-adverse),
        "expected_daily_pnl": _m(net),
        "note": "SIMULATION — no live order placed (hard rule #1)",
    }


def yield_curve(sizes: list[Decimal], tightness: list[int], base: MMScenario) -> list[dict]:
    """Expected-yield-vs-tightness/size table; adverse-selection separated out."""
    rows = []
    for size in sizes:
        for k in tightness:
            from dataclasses import replace
            rows.append(simulate(replace(base, size_usd=size, inside_spread_cents=k)))
    return rows


def _m(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))
