from __future__ import annotations

import math
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


def _finalize_probs(probs: dict[str, float]) -> dict[str, Decimal]:
    """q6 a float probability vector and push the rounding residual onto the largest entry,
    so it sums to exactly 1.000000 — the shared tail of every de-vig method."""
    normalized = {outcome: q6(Decimal(str(p))) for outcome, p in probs.items()}
    residual = Decimal("1.000000") - sum(normalized.values(), Decimal("0"))
    if residual:
        largest = max(normalized, key=normalized.__getitem__)
        normalized[largest] = q6(normalized[largest] + residual)
    return normalized


def devig_book(outcome_to_decimal_odds: dict[str, Decimal]) -> dict[str, Decimal]:
    """Proportional (multiplicative) de-vig: p_i = (1/o_i) / Σ(1/o_j). The crudest method —
    it overprices favorites and underprices longshots vs Shin/power. Decimal-exact (the
    historical default); power/shin compute in float internally."""
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


def devig_book_power(outcome_to_decimal_odds: dict[str, Decimal]) -> dict[str, Decimal]:
    """Power de-vig: find k with Σ r_i^k = 1 (r_i = 1/o_i), then p_i = r_i^k. Assigns a
    lower probability to longshots than proportional, correcting favorite-longshot bias."""
    rs = {o: 1.0 / float(odds) for o, odds in outcome_to_decimal_odds.items()}
    if sum(rs.values()) <= 0:
        raise ValueError("Book has no positive implied probabilities")
    lo, hi = 1e-4, 100.0  # Σr^k decreases in k; overround>1 => root k>1
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if sum(r ** mid for r in rs.values()) - 1.0 > 0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    return _finalize_probs({o: r ** k for o, r in rs.items()})


def devig_book_shin(outcome_to_decimal_odds: dict[str, Decimal]) -> dict[str, Decimal]:
    """Shin (1992) de-vig: models a fraction z of insider money and backs out the true
    probabilities. Like power it down-weights longshots, but via an insider-trading model."""
    rs = [1.0 / float(odds) for odds in outcome_to_decimal_odds.values()]
    keys = list(outcome_to_decimal_odds.keys())
    s = sum(rs)
    if s <= 0:
        raise ValueError("Book has no positive implied probabilities")

    def probs_for(z: float) -> list[float]:
        z = min(z, 0.999999)
        return [(math.sqrt(z * z + 4 * (1 - z) * r * r / s) - z) / (2 * (1 - z)) for r in rs]

    lo, hi = 0.0, 0.999999  # Σp(z) decreases from sqrt(S)>1 at z=0 toward 1
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if sum(probs_for(mid)) - 1.0 > 0:
            lo = mid
        else:
            hi = mid
    z = (lo + hi) / 2.0
    return _finalize_probs(dict(zip(keys, probs_for(z))))


DEVIG_METHODS = {
    "proportional": devig_book,
    "power": devig_book_power,
    "shin": devig_book_shin,
}


def devig_book_method(outcome_to_decimal_odds: dict[str, Decimal], method: str = "proportional") -> dict[str, Decimal]:
    return DEVIG_METHODS.get(method, devig_book)(outcome_to_decimal_odds)


def consensus_outcome_key(market_type: str, line: Decimal | None, outcome_label: str) -> str:
    # Totals consensus is only comparable on the exact same line (8.5 ≠ 9.0).
    if market_type == "TOTAL" and line is not None:
        return f"{outcome_label}:{line}"
    return outcome_label


def consensus_probabilities(quotes: list[Quote], min_books: int, min_books_totals: int | None = None,
                            method: str = "proportional", outlier_points: Decimal | None = None) -> dict[str, Decimal]:
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
        # pass3#5: reject implausible odds (mis-scaled/stale glitches) before they reach the
        # median. Plausible implied prob is ~[0.002, 0.998] (decimal odds ~[1.002, 500]).
        if quote.venue != "oddsapi" or quote.raw_price < Decimal("1.002") or quote.raw_price > Decimal("500"):
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
        devigged = [devig_book_method(odds, method) for odds in books if frozenset(odds) == complete]
        devigged = _drop_book_outliers(devigged, complete, outlier_points)  # pass3#5
        per_outcome: dict[str, list[Decimal]] = defaultdict(list)
        for book_probs in devigged:
            for outcome, prob in book_probs.items():
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


def _drop_book_outliers(devigged: list[dict[str, Decimal]], complete: frozenset,
                        outlier_points: Decimal | None) -> list[dict[str, Decimal]]:
    """pass3#5 / live §1.7: drop a book whose de-vigged prob for any outcome is more than
    outlier_points from that outcome's cross-book median (a stale/mis-scaled book). Off when
    outlier_points is None; never drops below 2 surviving books."""
    if outlier_points is None or len(devigged) <= 2:
        return devigged
    med = {o: Decimal(str(median([b[o] for b in devigged]))) for o in complete}
    kept = [b for b in devigged if all(abs(b[o] - med[o]) <= outlier_points for o in complete)]
    return kept if len(kept) >= 2 else devigged


def consensus_dispersion(quotes: list[Quote], min_books: int, method: str = "proportional") -> dict[str, Decimal]:
    """EV#3 / pass3#2: per-outcome cross-book dispersion (max-min spread of de-vigged probs)
    as a confidence signal — wide book disagreement should widen e_min / shrink trust, not be
    silently swallowed by the median. Keyed like consensus_probabilities."""
    raw_books: dict[tuple[str, str, Decimal | None], dict[str, Decimal]] = defaultdict(dict)
    for quote in quotes:
        if quote.venue != "oddsapi" or quote.raw_price < Decimal("1.002") or quote.raw_price > Decimal("500"):
            continue
        key = consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label)
        raw_books[(quote.book, quote.market_type, quote.line)][key] = quote.raw_price
    by_market: dict[tuple[str, Decimal | None], list[dict[str, Decimal]]] = defaultdict(list)
    for (_book, market_type, line), odds in raw_books.items():
        by_market[(market_type, line)].append(odds)
    dispersion: dict[str, Decimal] = {}
    for (_market_type, _line), books in by_market.items():
        complete = max((frozenset(o) for o in books), key=len)
        if len(complete) < 2:
            continue
        per_outcome: dict[str, list[Decimal]] = defaultdict(list)
        for odds in books:
            if frozenset(odds) == complete:
                for outcome, prob in devig_book_method(odds, method).items():
                    per_outcome[outcome].append(prob)
        for outcome, probs in per_outcome.items():
            if len(probs) >= min_books:
                dispersion[outcome] = q6(max(probs) - min(probs))
    return dispersion


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
