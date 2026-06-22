"""Intra-market arbitrage — the safest political arb there is.

On a single binary contract, exactly one of YES / NO resolves to $1. If you can BUY both
sides for less than $1 all-in (each side's ask + that side's fee), the payout is locked
regardless of outcome. There is NO matching step and NO cross-venue settlement risk: it
is one market under one rulebook. The only ways it fails are a stale quote (the ask is
gone when you try) or the whole market voiding (both sides refunded → net zero, not a
loss). That makes it strictly safer than any cross-venue arb.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb.pricing import all_in_fee_cost, q6


@dataclass(frozen=True)
class IntraMarketArb:
    ticker: str
    title: str
    category: str
    yes_ask: Decimal
    no_ask: Decimal
    yes_fee: Decimal
    no_fee: Decimal
    total_cost: Decimal      # all-in cost to own both sides
    roi: Decimal             # locked return = 1/total_cost - 1
    close_time: str


def find_intramarket_arb(
    market: dict,
    fee_rate: Decimal,
    category: str,
    taker_buffer: Decimal = Decimal("0"),
) -> IntraMarketArb | None:
    """Return a locked intra-market arb for one Kalshi market, or None."""
    yes_ask = _ask(market.get("yes_ask_dollars"))
    no_ask = _ask(market.get("no_ask_dollars"))
    if yes_ask is None or no_ask is None:
        return None
    # Empty-book trap (SPEC §3.1): ask 1.0 + bid 0.0 + liquidity 0 is not a real quote.
    if _is_empty_book(market):
        return None
    yes_fee = all_in_fee_cost(yes_ask, fee_rate, taker_buffer)
    no_fee = all_in_fee_cost(no_ask, fee_rate, taker_buffer)
    total = q6(yes_ask + yes_fee + no_ask + no_fee)
    if total >= Decimal("1") or total <= 0:
        return None
    return IntraMarketArb(
        ticker=str(market["ticker"]),
        title=str(market.get("title", "")),
        category=category,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_fee=yes_fee,
        no_fee=no_fee,
        total_cost=total,
        roi=q6(Decimal("1") / total - Decimal("1")),
        close_time=str(market.get("close_time", "")),
    )


def _ask(value) -> Decimal | None:
    if value is None:
        return None
    price = Decimal(str(value))
    return price if Decimal("0") < price < Decimal("1") else None


def _is_empty_book(market: dict) -> bool:
    yes_ask = Decimal(str(market.get("yes_ask_dollars") or "1"))
    yes_bid = Decimal(str(market.get("yes_bid_dollars") or "0"))
    liquidity = Decimal(str(market.get("liquidity_dollars") or "0"))
    return yes_ask >= 1 and yes_bid <= 0 and liquidity <= 0
