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


def correlation_scaled_stakes(stakes: list[tuple[str, Decimal]], rho: Decimal,
                              group_cap_fraction: Decimal, bankroll: Decimal) -> dict[int, Decimal]:
    """B8: correlation-aware sizing for a GROUP of simultaneous EV bets that share risk (same
    event, same team across markets, ...). Independent Kelly over-bets correlated outcomes:
    n positively-correlated bets sized as if independent exceed true Kelly and concentrate
    drawdown. Two corrections, both no-ops at the defaults so single bets are unchanged:

      1. Correlation haircut — scale every stake in the group by 1/(1 + rho*(n-1)), the factor
         that brings the summed variance of n equicorrelated unit bets back toward one unit.
      2. Group exposure cap — if the haircut group still exceeds group_cap_fraction*bankroll,
         scale the whole group down pro-rata so the joint stake respects one shared cap.

    `stakes` is [(id, stake_usd)]; returns {index: adjusted_stake}. rho<=0 and a group of one
    leave stakes untouched."""
    n = len(stakes)
    out: dict[int, Decimal] = {i: money(s) for i, (_id, s) in enumerate(stakes)}
    if n == 0:
        return out
    r = max(Decimal("0"), min(rho, Decimal("0.999")))
    if n > 1 and r > 0:
        haircut = Decimal("1") / (Decimal("1") + r * (n - 1))
        out = {i: money(s * haircut) for i, s in out.items()}
    total = sum(out.values(), Decimal("0"))
    cap = group_cap_fraction * bankroll
    if cap > 0 and total > cap and total > 0:
        scale = cap / total
        out = {i: money(s * scale) for i, s in out.items()}
    return out


def group_correlated_stakes(opps: list, rho: Decimal, group_cap_fraction: Decimal,
                            bankroll: Decimal) -> dict[int, Decimal]:
    """B8 driver: group Opportunity-like rows by shared event and apply correlation_scaled_stakes
    within each group. An opp must expose `.event.event_key` (or `.event_key`) and `.kelly_stake`.
    Groups of one (and rho<=0) return the original kelly_stake. Returns {id(opp): adjusted}."""
    by_event: dict[str, list[tuple[int, Decimal]]] = {}
    for opp in opps:
        ev = getattr(opp, "event", None)
        key = getattr(ev, "event_key", None) if ev is not None else getattr(opp, "event_key", None)
        stake = getattr(opp, "kelly_stake", None)
        if key is None or stake is None:
            continue
        by_event.setdefault(str(key), []).append((id(opp), stake))
    adjusted: dict[int, Decimal] = {}
    for _key, members in by_event.items():
        scaled = correlation_scaled_stakes([(str(i), s) for i, s in members], rho,
                                           group_cap_fraction, bankroll)
        for pos, (oid, _s) in enumerate(members):
            adjusted[oid] = scaled[pos]
    return adjusted
