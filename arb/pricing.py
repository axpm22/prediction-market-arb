from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from statistics import median

from .models import Quote

SIX_PLACES = Decimal("0.000001")
CENT = Decimal("0.01")


def q6(value: Decimal) -> Decimal:
    return value.quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ceil_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_CEILING)


def implied_from_decimal_odds(odds: Decimal) -> Decimal:
    if odds <= 0:
        raise ValueError("Decimal odds must be positive")
    return Decimal("1") / odds


def devig_book(outcome_to_decimal_odds: dict[str, Decimal]) -> dict[str, Decimal]:
    implied = {outcome: implied_from_decimal_odds(odds) for outcome, odds in outcome_to_decimal_odds.items()}
    overround = sum(implied.values(), Decimal("0"))
    if overround <= 0:
        raise ValueError("Book has no positive implied probabilities")
    normalized = {outcome: q6(prob / overround) for outcome, prob in implied.items()}
    residual = Decimal("1.000000") - sum(normalized.values(), Decimal("0"))
    if residual:
        largest = max(normalized, key=normalized.__getitem__)
        normalized[largest] = q6(normalized[largest] + residual)
    return normalized


def consensus_outcome_key(market_type: str, line: Decimal | None, outcome_label: str) -> str:
    # Totals consensus is only comparable on the exact same line (8.5 ≠ 9.0).
    if market_type == "TOTAL" and line is not None:
        return f"{outcome_label}:{line}"
    return outcome_label


def consensus_probabilities(quotes: list[Quote], min_books: int) -> dict[str, Decimal]:
    by_market: dict[tuple[str, str, Decimal | None], dict[str, Decimal]] = defaultdict(dict)
    for quote in quotes:
        if quote.venue != "oddsapi" or quote.raw_price <= 0:
            continue
        group = (quote.book, quote.market_type, quote.line)
        by_market[group][consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label)] = quote.raw_price

    per_outcome: dict[str, list[Decimal]] = defaultdict(list)
    for book_odds in by_market.values():
        if len(book_odds) < 2:
            continue
        for outcome, prob in devig_book(book_odds).items():
            per_outcome[outcome].append(prob)

    consensus: dict[str, Decimal] = {}
    for outcome, probs in per_outcome.items():
        if len(probs) >= min_books:
            consensus[outcome] = q6(Decimal(str(median(probs))))
    return consensus


def kalshi_fee_cost(price: Decimal, fee_rate: Decimal) -> Decimal:
    return q6(fee_rate * price * (Decimal("1") - price))


def taker_fee_cost(price: Decimal, fee_rate: Decimal) -> Decimal:
    return q6(fee_rate * price * (Decimal("1") - price))


def all_in_fee_cost(price: Decimal, fee_rate: Decimal, taker_buffer: Decimal = Decimal("0")) -> Decimal:
    """Per-contract friction used for decisioning: venue taker fee plus a price buffer.

    `taker_buffer` is an additive cost in contract-price units. A value of 0.005 means
    half a cent per $1 payout contract, guarding against slippage, queue position, stale
    top-of-book reads, and small unmodeled platform/transfer frictions.
    """
    return q6(taker_fee_cost(price, fee_rate) + taker_buffer)


def kalshi_fee_money(contracts: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    return ceil_cent(fee_rate * contracts * price * (Decimal("1") - price))


def expected_value(consensus_probability: Decimal, effective_cost: Decimal) -> Decimal:
    if effective_cost <= 0:
        raise ValueError("Effective cost must be positive")
    return q6((consensus_probability - effective_cost) / effective_cost)


def edge_points(consensus_probability: Decimal, effective_cost: Decimal) -> Decimal:
    """EV_SPEC §3.1: edge in probability points = p - a (the unit E_min is set in)."""
    return q6(consensus_probability - effective_cost)


def kelly_fraction(consensus_probability: Decimal, effective_cost: Decimal, kelly_multiplier: Decimal) -> Decimal:
    """EV_SPEC §7.1: for a $1-if-true contract at ask a, true prob p, full-Kelly fraction
    simplifies to (p - a)/(1 - a). Bet a fraction of it (quarter–half) because p is
    estimated, not known (§7.2). Returns 0 when there is no positive edge."""
    if not Decimal("0") < effective_cost < Decimal("1"):
        return Decimal("0")
    full = (consensus_probability - effective_cost) / (Decimal("1") - effective_cost)
    if full <= 0:
        return Decimal("0")
    return q6(kelly_multiplier * full)


def clv_points(venue_close_mid: Decimal, entry_ask: Decimal) -> Decimal:
    """EV_SPEC §5.1: closing-line value = venue's own closing mid - entry ask, both in
    the $1 unit. Positive means the venue drifted toward (past) your entry — the
    slow-gradient claim realized, measurable with zero settled games."""
    return q6(venue_close_mid - entry_ask)


def capital_charge(days_to_settlement: float, annual_rate: Decimal) -> Decimal:
    """Opportunity cost of locking capital until settlement. An arb's ROI is earned over
    the holding period, so it must clear what the same capital would earn elsewhere over
    those days; fees are already inside the cost, this adds the time-value of money."""
    days = Decimal(str(max(0.0, days_to_settlement)))
    return q6(annual_rate * days / Decimal("365"))


def is_economic(roi: Decimal, days_to_settlement: float, annual_rate: Decimal) -> bool:
    """An arb is economic only if its locked ROI beats the capital charge for its holding
    period. A 0.2% lock held 14 days is worse than cash at 5%/yr."""
    return roi > capital_charge(days_to_settlement, annual_rate)
