"""Sizing math (SPEC §13.4, addendum §3) — projection columns, NEVER headline.

All money math in Decimal (rule 5). Fills price at ladder VWAP, never
top-of-book. Caps: min(Kelly term, haircut depth / divisor, q*), where q* is
the largest size whose VWAP slippage stays within slippage.max_edge_fraction
of net edge.
"""
from __future__ import annotations

import ast
from decimal import Decimal

from arb.models import q6


def parse_ladder(ladder_json: str | None) -> list[tuple[Decimal, Decimal]]:
    """[(price, size), ...] as Decimals. Accepts both the JSON form
    (polymarket) and the str-repr form (sx). Unparseable/empty -> []."""
    if not ladder_json:
        return []
    try:
        levels = ast.literal_eval(ladder_json)
        return [(Decimal(str(p)), Decimal(str(s))) for p, s in levels]
    except (ValueError, SyntaxError, TypeError):
        return []


def vwap_cost(ladder, contracts: Decimal) -> tuple[Decimal | None, Decimal]:
    """(vwap, filled) walking `contracts` up the ladder. Partial fill if the
    ladder exhausts — depth is never invented (V1 bug B1)."""
    filled, cost = Decimal(0), Decimal(0)
    for price, size in ladder:
        take = min(size, contracts - filled)
        filled += take
        cost += price * take
        if filled >= contracts:
            break
    return (q6(cost / filled) if filled else None), filled


def q_star(ladder, net_edge: Decimal, max_edge_fraction: Decimal) -> Decimal:
    """Largest contract count whose VWAP slippage vs top-of-book stays within
    max_edge_fraction x net_edge. VWAP is monotone in size, so walk levels and
    solve the one linear boundary."""
    if not ladder or net_edge <= 0:
        return Decimal(0)
    p0 = ladder[0][0]
    bound = p0 + net_edge * max_edge_fraction
    filled, cost = Decimal(0), Decimal(0)
    for price, size in ladder:
        if (cost + price * size) / (filled + size) <= bound:
            filled += size
            cost += price * size
            continue
        # failing the whole-level check implies price > bound (mixture rule),
        # so the denominator is positive: solve vwap(n) = bound
        return q6((price * filled - cost) / (price - bound))
    return q6(filled)


def kelly_stake(p: Decimal, c: Decimal, bankroll: Decimal,
                kelly_fraction: Decimal, cap_frac: Decimal) -> Decimal:
    """Binary-market f* = (p - c)/(1 - c) with c fee-inclusive (addendum §3);
    fractional Kelly, hard-capped at cap_frac of bankroll. Projection only —
    admissible p does not exist today (EV lane frozen)."""
    if p <= c:
        return Decimal(0)
    f = (p - c) / (1 - c)
    return q6(min(f * kelly_fraction, cap_frac) * bankroll)


def arb_projection(cfg, legs: list[dict], quotes: list[dict],
                   ladders: list[list]) -> dict | None:
    """Projected size/P&L for one locked position:
    contracts = min(bankroll-cap Kelly term, haircut depth/divisor, q* per leg).
    A locked payout has no p, so the hard bankroll cap IS the Kelly term.
    Fees held at entry-time fee_cost (fee-at-VWAP needs the fee curve — a
    versioned change to every cost vector, per execution.slippage_model)."""
    costs = [Decimal(lg["cost"]) + Decimal(lg["fee_cost"]) for lg in legs]
    cost_sum = sum(costs)
    net_edge = 1 - cost_sum
    if net_edge <= 0:
        return None
    ex, sl, sz = cfg["execution"], cfg["slippage"], cfg["sizing"]
    caps = [sz["max_kelly_fraction_of_bankroll"] * cfg["bankroll_usd"] / cost_sum]
    for lg, qrow, ladder in zip(legs, quotes, ladders):
        depth = Decimal(qrow["depth_usd"]) * ex["fillable_fraction"]
        caps.append(depth / sl["depth_divisor"] / Decimal(lg["cost"]))
        caps.append(q_star(ladder, net_edge, sl["max_edge_fraction"]))
    contracts = q6(min(caps))
    if contracts <= 0:
        return None
    vwap_sum = Decimal(0)
    for lg, ladder in zip(legs, ladders):
        v, filled = vwap_cost(ladder, contracts)
        if v is None or filled < contracts:
            return None  # ladder can't fill the projected size
        vwap_sum += v + Decimal(lg["fee_cost"])
    fixed = sum(Decimal(str(ex["fixed_cost_per_fill_usd"].get(lg["venue"], 0)))
                for lg in legs)
    return {"contracts": contracts,
            "stake_usd": q6(contracts * vwap_sum),
            "net_edge": q6(net_edge),
            "pnl_usd": q6(contracts * (1 - vwap_sum) - fixed)}
