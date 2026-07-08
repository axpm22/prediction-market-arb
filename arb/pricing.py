"""De-vig, EV, arb math — the SPEC §7 vectors live here.

Everything Decimal at full precision; quantize (q6, ROUND_HALF_EVEN) only at
store/compare boundaries. Floats appear only inside power/shin root-finding and
are immediately re-quantized (rule 5).
"""
from __future__ import annotations

from decimal import Decimal

from arb.models import q6


def _proportional(qs: list[Decimal]) -> list[Decimal]:
    total = sum(qs)
    return [q6(q / total) for q in qs]


def _power(qs: list[Decimal]) -> list[Decimal]:
    # p_i = q_i^k with k solving sum(p) = 1; float bisection, re-quantized
    fq = [float(q) for q in qs]

    def total(k):
        return sum(x ** k for x in fq)

    lo, hi = 0.5, 3.0
    while total(hi) > 1:
        hi *= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if total(mid) > 1:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    return [q6(Decimal(x ** k)) for x in fq]


def _shin(qs: list[Decimal]) -> list[Decimal]:
    # Shin (1993) insider-trading model: solve z so probabilities sum to 1
    fq = [float(q) for q in qs]
    big_q = sum(fq)

    def probs(z):
        return [((z ** 2 + 4 * (1 - z) * x ** 2 / big_q) ** 0.5 - z) / (2 * (1 - z))
                for x in fq]

    lo, hi = 0.0, 0.3
    for _ in range(200):
        mid = (lo + hi) / 2
        if sum(probs(mid)) > 1:   # sum is decreasing in z
            lo = mid
        else:
            hi = mid
    return [q6(Decimal(p)) for p in probs((lo + hi) / 2)]


_METHODS = {"proportional": _proportional, "power": _power, "shin": _shin}


def devig(odds: list[Decimal], method: str = "power") -> list[Decimal]:
    """Decimal odds -> de-vigged probabilities. Method is config-selected;
    `power` is a coin-flip default, not an empirical winner (§7.2)."""
    qs = [1 / Decimal(o) for o in odds]
    return _METHODS[method](qs)


def ev(p_hat: Decimal, effective_cost: Decimal) -> Decimal:
    """EV = (p_hat - c)/c with c = cost + fee_cost. ROI form, display/threshold use;
    edge_points for EV kinds is p_hat - c (§7.1)."""
    return (p_hat - effective_cost) / effective_cost


def true_arb_sum(effective_costs: list[Decimal]) -> Decimal:
    """S = sum of best effective cost per outcome; S < 1 is a locked basket.
    S is a stored/compared quantity, so it quantizes here (v3.4 boundary rule) —
    the pinned §7.2 ROI vector 0.037350 derives from the quantized S=0.963995."""
    return q6(sum(effective_costs))


def stakes_for(bankroll: Decimal, effective_costs: list[Decimal]) -> list[Decimal]:
    """stake_i = B*c_i/S  <=> equal contract counts B/S per leg — the identity that
    locks 2-leg arbs, YES baskets, and NO baskets alike (§7.2)."""
    s = true_arb_sum(effective_costs)
    return [bankroll * c / s for c in effective_costs]


def basket_edge(effective_cost_sum: Decimal, payout: Decimal) -> Decimal:
    """edge_points for locked kinds: 1 - S/P, P = payout per unit basket
    (1 for 2-leg arbs and YES baskets, N-1 for NO baskets) — §7.1."""
    return 1 - effective_cost_sum / payout


def divergence_spread(mid_a: Decimal, mid_b: Decimal) -> Decimal:
    """Raw-mid spread (no fees — a signal, not a P&L claim)."""
    return mid_a - mid_b


def frechet_bounds(leg_probs: list[Decimal]) -> tuple[Decimal, Decimal]:
    lower = max(Decimal(0), sum(leg_probs) - (len(leg_probs) - 1))
    return lower, min(leg_probs)


def independence_fair(leg_probs: list[Decimal]) -> Decimal:
    fair = Decimal(1)
    for p in leg_probs:
        fair *= p
    return fair
