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


def consensus_probabilities(quotes: list[Quote], min_books: int, min_books_totals: int | None = None) -> dict[str, Decimal]:
    """Median de-vigged consensus per market, with two correctness guards:

    #2 only books carrying the COMPLETE outcome set vote — a book missing the soccer DRAW
       or one side of a total would otherwise be de-vigged as a 2-/1-way and inflate.
    #1 after taking per-outcome medians, RENORMALIZE the vector so each market sums to 1.
       Independent medians don't, so p(HOME)+p(AWAY) could be 0.97 or 1.04 and bias EV.
    A market only contributes if every outcome in its complete set cleared min_books.
    """
    # (book, market_type, line) -> {outcome_key: decimal_odds}
    raw_books: dict[tuple[str, str, Decimal | None], dict[str, Decimal]] = defaultdict(dict)
    for quote in quotes:
        if quote.venue != "oddsapi" or quote.raw_price <= 0:
            continue
        key = consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label)
        raw_books[(quote.book, quote.market_type, quote.line)][key] = quote.raw_price

    # Group books by market and find that market's complete outcome set (max cardinality seen).
    books_by_market: dict[tuple[str, Decimal | None], list[dict[str, Decimal]]] = defaultdict(list)
    for (_book, market_type, line), odds in raw_books.items():
        books_by_market[(market_type, line)].append(odds)

    totals_min = min_books_totals if min_books_totals is not None else min_books
    consensus: dict[str, Decimal] = {}
    for (market_type, _line), books in books_by_market.items():
        complete = max((frozenset(o) for o in books), key=len)
        if len(complete) < 2:
            continue  # a single-outcome market can't be de-vigged
        per_outcome: dict[str, list[Decimal]] = defaultdict(list)
        for odds in books:
            if frozenset(odds) != complete:  # #2: incomplete books don't vote
                continue
            for outcome, prob in devig_book(odds).items():
                per_outcome[outcome].append(prob)
        # #3 totals are line-fragmented (books rarely share the exact line), so a 3-book
        # gate makes totals consensus nearly always dead. Use a lower totals threshold —
        # NOT half-point interpolation, which would change the event definition.
        gate = totals_min if market_type == "TOTAL" else min_books
        if any(len(per_outcome.get(o, [])) < gate for o in complete):
            continue
        medians = {o: Decimal(str(median(per_outcome[o]))) for o in complete}
        total = sum(medians.values(), Decimal("0"))
        if total <= 0:
            continue
        for outcome, value in medians.items():  # #1: renormalize the median vector to 1
            consensus[outcome] = q6(value / total)
    return consensus


def kalshi_fee_cost(price: Decimal, fee_rate: Decimal) -> Decimal:
    """Kalshi's risk-based fee: rate·p·(1−p), peaks at p=0.5. Specific to Kalshi."""
    return q6(fee_rate * price * (Decimal("1") - price))


def linear_fee_cost(price: Decimal, fee_rate: Decimal) -> Decimal:
    """Flat percentage of trade notional (e.g. Polymarket's taker fee): rate·price.
    NOT the quadratic curve — applying Kalshi's p(1−p) here under-charged Polymarket
    fees and mispriced every Polymarket edge."""
    return q6(fee_rate * price)


# Each venue's fee is modeled on its own basis; the bug was using Kalshi's curve for all.
FEE_MODELS = {"quadratic": kalshi_fee_cost, "linear": linear_fee_cost}


def all_in_fee_cost(price: Decimal, fee_rate: Decimal, taker_buffer: Decimal = Decimal("0"),
                    basis: str = "linear") -> Decimal:
    """Per-contract friction for decisioning: venue taker fee (on its correct basis) plus a
    price buffer. `taker_buffer` is additive in contract-price units (0.005 = half a cent
    per $1 payout) guarding slippage/queue/stale-read frictions. `basis` selects the fee
    curve: 'quadratic' for Kalshi, 'linear' for Polymarket/SX."""
    model = FEE_MODELS.get(basis, linear_fee_cost)
    return q6(model(price, fee_rate) + taker_buffer)


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
