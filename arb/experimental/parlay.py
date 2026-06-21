"""Phase 7 parlay math (SPEC §11). Pure functions; nothing here touches the pipeline.

A parlay pays $1 iff all legs win. There is no static replication, so these are EV
signals only — always REVIEW, never auto-filled.
"""
from __future__ import annotations

from decimal import Decimal

ZERO = Decimal("0")
ONE = Decimal("1")


def frechet_bounds(leg_probs: list[Decimal]) -> tuple[Decimal, Decimal]:
    """Model-free bounds on P(all legs win) for legs priced by the same venue.

    max(0, Σp − (n−1)) ≤ P(all) ≤ min(p) holds under ANY correlation structure.
    """
    if not leg_probs:
        raise ValueError("Parlay needs at least one leg")
    lower = max(ZERO, sum(leg_probs, ZERO) - Decimal(len(leg_probs) - 1))
    return lower, min(leg_probs)


def independence_fair(leg_probs: list[Decimal]) -> Decimal:
    """Πp — valid as a baseline for cross-game legs only (SPEC §11 class 2)."""
    fair = ONE
    for p in leg_probs:
        fair *= p
    return fair


def classify_parlay_ask(
    ask: Decimal,
    leg_probs: list[Decimal],
    divergence_threshold: Decimal,
    same_game: bool,
) -> str | None:
    """Return the SPEC §11 signal class for a parlay ask, or None.

    - 'frechet-underpriced': ask below the lower Fréchet bound — cheap under any
      correlation. Report at any size.
    - 'independence-cheap': cross-game only; ask below Πp by ≥ threshold.
      Same-game legs are positively correlated (fair > Πp), so the product is not
      a bound there — annotate direction only, never signal.
    """
    lower, _upper = frechet_bounds(leg_probs)
    if ask < lower:
        return "frechet-underpriced"
    if not same_game and ask <= independence_fair(leg_probs) - divergence_threshold:
        return "independence-cheap"
    return None


def classify_parlay_bid(bid: Decimal, leg_probs: list[Decimal]) -> str | None:
    """Bid above the upper Fréchet bound = overpriced under any correlation."""
    _lower, upper = frechet_bounds(leg_probs)
    if bid > upper:
        return "frechet-overpriced"
    return None
