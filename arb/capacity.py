"""Microstructure arb capacity — how much can you stake before the lock goes negative.

An arb buys equal payout N on each leg; walking each leg's depth ladder raises its average
fill price, so the lock holds only while Σ vwap_i(N) < 1. This computes, from the captured
ladders (Wave 2): the break-even size (where the next dollar loses money), the profit-max
size (where marginal margin = marginal slippage), and whether the binding constraint is the
EDGE (you'd hit break-even) or DEPTH (the book runs out while still profitable). This is the
real version of `capturable_lower_bound`'s min(depth)×ROI floor.

Pure and measurement-only; nothing here places or sizes a live order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .pricing import money, q6

Ladder = list[tuple[Decimal, Decimal]]  # (effective cost per $1 payout, depth_usd) best first


@dataclass(frozen=True)
class ArbCapacity:
    feasible: bool
    breakeven_stake_usd: Decimal     # total $ staked at which profit hits 0; beyond it you lose
    profitmax_stake_usd: Decimal     # total $ staked that maximizes locked profit
    max_profit_usd: Decimal          # locked profit at the profit-max size
    fillable_stake_usd: Decimal      # total $ to consume the binding (thinnest) book
    limited_by: str                  # 'edge' | 'depth' | 'none'


def _fill(ladder: Ladder, contracts: Decimal) -> Decimal:
    """$ staked to buy `contracts` walking the ladder (assumes contracts <= ladder capacity)."""
    filled = Decimal("0")
    stake = Decimal("0")
    for cost, depth in ladder:
        if cost <= 0 or depth <= 0:
            continue
        avail = depth / cost
        take = min(contracts - filled, avail)
        if take <= 0:
            break
        filled += take
        stake += take * cost
        if filled >= contracts:
            break
    return stake


def _capacity_contracts(ladder: Ladder) -> Decimal:
    return sum((depth / cost for cost, depth in ladder if cost > 0 and depth > 0), Decimal("0"))


def arb_capacity(ladders: list[Ladder], haircut_per_contract: Decimal = Decimal("0")) -> ArbCapacity:
    """`haircut_per_contract` (K4): a per-$1-payout cost added on top of the ladders — the
    C1 skew·velocity simultaneity haircut — so sizing consumes the ADJUSTED edge, not the
    raw one. 0 = legacy behavior."""
    zero = Decimal("0.00")
    ladders = [l for l in ladders if l]
    if len(ladders) < 2:
        return ArbCapacity(False, zero, zero, zero, zero, "none")
    n_max = min(_capacity_contracts(l) for l in ladders)  # binding (thinnest) book in contracts
    if n_max <= 0:
        return ArbCapacity(False, zero, zero, zero, zero, "none")

    # Profit(N) = N − Σ stake_i(N) is piecewise-linear & concave, so its max and zero-crossing
    # sit at level boundaries. Evaluate at every leg's cumulative-contract breakpoint.
    points = {Decimal("0"), n_max}
    for ladder in ladders:
        cum = Decimal("0")
        for cost, depth in ladder:
            if cost > 0 and depth > 0:
                cum += depth / cost
                if cum < n_max:
                    points.add(cum)
    grid = sorted(points)

    def profit(n: Decimal) -> Decimal:
        return n * (Decimal("1") - haircut_per_contract) - sum((_fill(l, n) for l in ladders), Decimal("0"))

    evals = [(n, profit(n)) for n in grid]
    best_n, best_p = max(evals, key=lambda x: x[1])
    fillable_stake = sum((_fill(l, n_max) for l in ladders), Decimal("0"))

    # Break-even: the size where profit crosses 0. If profit at the fully-filled book is still
    # positive, the book runs out before break-even -> depth-limited; else edge-limited.
    breakeven = n_max
    limited_by = "depth"
    prev = evals[0]
    for n, p in evals[1:]:
        if prev[1] > 0 and p <= 0:
            np_, pp = prev
            breakeven = np_ + (n - np_) * pp / (pp - p) if pp != p else n
            limited_by = "edge"
            break
        prev = (n, p)
    if best_p <= 0:
        return ArbCapacity(False, zero, zero, zero, money(fillable_stake), "none")
    return ArbCapacity(
        True,
        money(breakeven),               # at break-even profit=0 so total stake == N
        money(best_n - best_p),         # stake at profit-max = N − profit
        money(best_p),
        money(fillable_stake),
        limited_by,
    )


def _leg_ladder(leg: dict, fillable_fraction: Decimal = Decimal("1")) -> Ladder:
    """Effective-cost ladder for a stored leg: prefer the captured ladder (raw cost + the leg's
    per-$1 fee), else a single top-of-book level from cost+fee_cost & depth_usd. B3: the
    fillable-fraction haircut scales each level's taker notional for SIZING only (displayed
    top-of-book is not 100% takeable); the stored book is untouched. 1.0 = no haircut."""
    fee = _dec(leg.get("fee_cost"))
    raw = leg.get("ladder")
    if raw:
        out: Ladder = []
        for level in raw:
            cost = _dec(level[0]) + fee
            depth = _dec(level[1]) * fillable_fraction
            if cost > 0 and depth > 0:
                out.append((cost, depth))
        if out:
            return out
    cost = _dec(leg.get("cost")) + fee
    depth = leg.get("depth_usd")
    if cost > 0 and depth not in (None, ""):
        return [(cost, _dec(depth) * fillable_fraction)]
    return []


def max_economic_arb_size(legs: list[dict], fillable_fraction: Decimal = Decimal("1")) -> ArbCapacity:
    """Capacity for a stored arb (legs from legs_json)."""
    return arb_capacity([_leg_ladder(leg, fillable_fraction) for leg in legs])


def _quote_ladder(quote, fillable_fraction: Decimal = Decimal("1")) -> Ladder:
    """Effective-cost ladder for a live Quote (raw ladder level + the leg's per-$1 fee). B3:
    the fillable-fraction haircut scales each level's taker notional for SIZING only (displayed
    top-of-book is not 100% takeable); the stored book is untouched. 1.0 = no haircut."""
    if quote.ladder:
        out = [(c + quote.fee_cost, d * fillable_fraction)
               for c, d in quote.ladder if (c + quote.fee_cost) > 0 and d > 0]
        if out:
            return out
    if quote.depth_usd and quote.depth_usd > 0:
        return [(quote.effective_cost, quote.depth_usd * fillable_fraction)]
    return []


def max_economic_arb_size_quotes(legs, fillable_fraction: Decimal = Decimal("1"),
                                 haircut_per_contract: Decimal = Decimal("0")) -> ArbCapacity:
    """Capacity for live Quote legs (used in detection)."""
    return arb_capacity([_quote_ladder(q, fillable_fraction) for q in legs], haircut_per_contract)


def ladder_scaled_stake_quotes(legs, bankroll: Decimal, max_stake_pct: Decimal,
                               safety_frac: Decimal = Decimal("0.5"),
                               fillable_fraction: Decimal = Decimal("1"),
                               haircut_per_contract: Decimal = Decimal("0")) -> Decimal:
    """Depth/edge-scaled total budget for live Quote legs: a `safety_frac` of the profit-max
    ladder size (which jointly encodes edge = margin below $1 and depth = how far you can walk
    each book before the lock crosses), capped by the bankroll per-bet limit. 0 = no ladder."""
    cap = max_economic_arb_size_quotes(legs, fillable_fraction, haircut_per_contract)
    if not cap.feasible or cap.profitmax_stake_usd <= 0:
        return Decimal("0")
    return money(min(cap.profitmax_stake_usd * safety_frac, bankroll * max_stake_pct))


def ladder_scaled_stake(legs: list[dict], bankroll: Decimal, max_stake_pct: Decimal,
                        safety_frac: Decimal = Decimal("0.5")) -> Decimal:
    """Depth/edge-scaled total budget: a fraction of the profit-max ladder size, capped by the
    bankroll per-bet limit. Returns 0 when no ladder is available (fall back to flat sizing)."""
    cap = max_economic_arb_size(legs)
    if not cap.feasible or cap.profitmax_stake_usd <= 0:
        return Decimal("0")
    return money(min(cap.profitmax_stake_usd * safety_frac, bankroll * max_stake_pct))


def _dec(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))
