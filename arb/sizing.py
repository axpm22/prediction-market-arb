"""Fractional-Kelly sizing — BUILD_PLAN Phase 3.

Flat-$1 stays the honest measurement headline. This is a PARALLEL projection: what
quarter-Kelly would have staked, to show a sizing-aware equity curve and drawdown.
Decimal throughout; never replaces the flat-$1 baseline.
"""
from __future__ import annotations

from decimal import Decimal

from .pricing import money, q6


def kelly_f_star(p_hat: Decimal, effective_cost: Decimal) -> Decimal:
    """Full-Kelly fraction for a $1-if-true binary bought at effective cost c.

    net odds b = (1-c)/c, q = 1-p; f* = (b·p - q)/b, which simplifies to (p-c)/(1-c).
    Negative f* (no edge) clips to 0 — Kelly says don't bet.
    """
    if not Decimal("0") < effective_cost < Decimal("1"):
        return Decimal("0")
    f = (p_hat - effective_cost) / (Decimal("1") - effective_cost)
    return q6(f) if f > 0 else Decimal("0")


def kelly_stake(p_hat: Decimal, effective_cost: Decimal, kelly_fraction: Decimal,
                bankroll: Decimal, max_fraction_of_bankroll: Decimal) -> Decimal:
    """kelly_fraction · f* · bankroll, hard-capped at max_fraction_of_bankroll · bankroll."""
    f_star = kelly_f_star(p_hat, effective_cost)
    if f_star <= 0:
        return Decimal("0.00")
    fraction = min(kelly_fraction * f_star, max_fraction_of_bankroll)
    return money(fraction * bankroll)
