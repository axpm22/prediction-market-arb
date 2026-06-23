"""Shared slippage application for adapters (Wave 2, does-well#2).

Each adapter captures a top-of-book ladder of (cost, depth_usd) levels; this turns it into a
taker buffer. 'flat' keeps the legacy constant buffer (no ledger-cost change); 'ladder' scales
it by how far filling a reference stake walks the book.
"""
from __future__ import annotations

from decimal import Decimal

from arb.pricing import depth_scaled_buffer


def effective_buffer(ladder: tuple[tuple[Decimal, Decimal], ...] | None, model: str,
                     ref_stake: Decimal, base_buffer: Decimal) -> Decimal:
    if model == "ladder" and ladder:
        return depth_scaled_buffer(list(ladder), ref_stake, base_buffer)
    return base_buffer
