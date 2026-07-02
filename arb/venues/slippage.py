"""Shared slippage application for adapters (Wave 2, does-well#2).

Each adapter captures a top-of-book ladder of (cost, depth_usd) levels; this turns it into a
taker buffer. 'flat' keeps the legacy constant buffer (no ledger-cost change); 'ladder' scales
it by how far filling a reference stake walks the book.
"""
from __future__ import annotations

from decimal import Decimal

from arb.pricing import all_in_fee_cost, depth_scaled_buffer, q6

# K1/K2 root cause: each venue's fee curve, in ONE place. The verifier re-priced Kalshi on
# the linear curve while entry pricing used the quadratic one; keeping the basis here means
# there is no second implementation of "what does this leg cost".
FEE_BASIS = {"kalshi": "quadratic", "polymarket": "linear", "sx": "linear"}


def effective_buffer(ladder: tuple[tuple[Decimal, Decimal], ...] | None, model: str,
                     ref_stake: Decimal, base_buffer: Decimal) -> Decimal:
    if model == "ladder" and ladder:
        return depth_scaled_buffer(list(ladder), ref_stake, base_buffer)
    return base_buffer


def leg_fee_cost(venue: str, price: Decimal, fee_rate: Decimal | None,
                 ladder: tuple[tuple[Decimal, Decimal], ...] | None,
                 slippage_model: str, slippage_ref_stake: Decimal, taker_buffer: Decimal,
                 win_fee_rate: Decimal | None = None) -> Decimal:
    """Per-$1 friction for one leg: venue fee on its correct basis + slippage buffer +
    (SX) the win fee. THE single leg-cost implementation — adapters price entries with it
    and the flash verifier re-quotes with it, so the two can never diverge (K1/K2)."""
    buffer = effective_buffer(ladder, slippage_model, slippage_ref_stake, taker_buffer)
    fee = all_in_fee_cost(price, fee_rate if fee_rate is not None else Decimal("0"),
                          buffer, basis=FEE_BASIS.get(venue, "linear"))
    if win_fee_rate is not None and win_fee_rate > 0:
        # D3: SX charges its fee only on winning bets; an arb always has one winning leg.
        fee = q6(fee + win_fee_rate * (Decimal("1") - price))
    return fee
