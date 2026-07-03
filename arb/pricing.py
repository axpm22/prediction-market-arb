from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from datetime import datetime, timezone
from statistics import median, stdev

from .models import Quote

SIX_PLACES = Decimal("0.000001")
CENT = Decimal("0.01")
_BISECT_TOL = 1e-9  # D20: power/shin bisection converges to <1e-6 well before 200 halvings


def _modal_complete_set(outcome_sets: list[frozenset]) -> frozenset:
    """C14: the consensus outcome set is the MOST COMMON complete set across books, not the
    longest. `max(..., key=len)` let a single stray book carrying an extra (mis-parsed/alt)
    outcome redefine the market and discard every correctly-formed book. Ties break toward the
    longer set, so the uniform-book case (every book the same set) is unchanged."""
    if not outcome_sets:
        return frozenset()
    counts: dict[frozenset, int] = defaultdict(int)
    for s in outcome_sets:
        counts[s] += 1
    return max(counts, key=lambda s: (counts[s], len(s)))


def _weighted_median(pairs: list[tuple[Decimal, int]]) -> Decimal:
    """C15: weight-aware median. The old sharp-book path duplicated a book's prob `weight`
    times before statistics.median, which at even counts could average a value with itself and
    emit a number no book quoted. This is the standard weighted median (lower-weighted-median
    at the 50% mass crossing). When all weights are equal it reduces EXACTLY to statistics.median
    (including the even-count midpoint average), so default (unweighted) behavior is unchanged."""
    if not pairs:
        raise ValueError("weighted median of empty sample")
    weights = {w for _v, w in pairs}
    if len(weights) == 1:  # equal weights -> identical to statistics.median (frozen behavior)
        return Decimal(str(median([v for v, _w in pairs])))
    ordered = sorted(pairs, key=lambda vw: vw[0])
    total = sum(w for _v, w in ordered)
    half = Decimal(total) / 2
    cum = 0
    for value, w in ordered:
        cum += w
        if Decimal(cum) >= half:
            return value
    return ordered[-1][0]


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
        if hi - lo < _BISECT_TOL:  # D20: converged to 1e-6; stop early (was always 200 halvings)
            break
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
        if hi - lo < _BISECT_TOL:  # D20: early exit on convergence
            break
    z = (lo + hi) / 2.0
    return _finalize_probs(dict(zip(keys, probs_for(z))))


DEVIG_METHODS = {
    "proportional": devig_book,
    "power": devig_book_power,
    "shin": devig_book_shin,
}


def devig_book_method(outcome_to_decimal_odds: dict[str, Decimal], method: str = "proportional") -> dict[str, Decimal]:
    return DEVIG_METHODS.get(method, devig_book)(outcome_to_decimal_odds)


def walk_ladder(levels: list[tuple[Decimal, Decimal]], stake_usd: Decimal) -> tuple[Decimal, Decimal]:
    """Walk a top-of-book ladder to fill `stake_usd` of $-notional and return
    (volume-weighted cost per $1 payout, filled $-notional). `levels` are (cost, depth_usd)
    best (lowest cost) first. If the ladder is too thin, filled < stake_usd (depth-capped)."""
    if not levels:
        return (Decimal("0"), Decimal("0"))
    filled = Decimal("0")
    contracts = Decimal("0")
    for cost, depth in levels:
        if cost <= 0 or depth <= 0:
            continue
        take = min(stake_usd - filled, depth)
        if take <= 0:
            break
        contracts += take / cost
        filled += take
        if filled >= stake_usd:
            break
    if contracts <= 0:
        return (levels[0][0], Decimal("0"))
    return (q6(filled / contracts), q6(filled))


def depth_scaled_buffer(levels: list[tuple[Decimal, Decimal]], stake_usd: Decimal,
                        base_buffer: Decimal) -> Decimal:
    """does-well#2 / EV#6: slippage as a function of stake/depth, not a flat constant. The
    buffer is how far the volume-weighted fill price for `stake_usd` is above the top of book,
    floored at base_buffer (latency/queue risk that exists even at zero size)."""
    if not levels:
        return base_buffer
    vwap, filled = walk_ladder(levels, stake_usd)
    top = levels[0][0]
    walked = (vwap - top) if filled > 0 else Decimal("0")
    return q6(max(base_buffer, walked))


def consensus_outcome_key(market_type: str, line: Decimal | None, outcome_label: str) -> str:
    # Any line-bearing market is only comparable on the exact same line (total 8.5 ≠ 9.0;
    # likewise a handicap/spread line is its own market). Key on the line whenever one is
    # present so different lines — or a moneyline vs a same-label handicap — never collide on
    # one consensus entry. (Today only WINNER[line=None] and TOTAL exist, so this is a no-op
    # for current markets and future-proofs the moment spreads are added.)
    if line is not None:
        return f"{outcome_label}:{line}"
    return outcome_label


def consensus_probabilities(quotes: list[Quote], min_books: int, min_books_totals: int | None = None,
                            method: str = "proportional", outlier_points: Decimal | None = None,
                            sharp_books: frozenset[str] | set[str] | None = None,
                            sharp_weight: int = 2) -> dict[str, Decimal]:
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

    # Group books by market (carry the book name so EV#2 can weight sharp books).
    books_by_market: dict[tuple[str, Decimal | None], list[tuple[str, dict[str, Decimal]]]] = defaultdict(list)
    for (book, market_type, line), odds in raw_books.items():
        books_by_market[(market_type, line)].append((book, odds))

    sharp_set = frozenset(sharp_books) if sharp_books else frozenset()
    weight = max(1, int(sharp_weight))
    totals_min = min_books_totals if min_books_totals is not None else min_books
    consensus: dict[str, Decimal] = {}
    for (market_type, _line), named_books in books_by_market.items():
        # C14: use the MODAL complete set, not the longest (a stray extra-outcome book must not
        # redefine the market and discard every well-formed book).
        complete = _modal_complete_set([frozenset(o) for _b, o in named_books])
        if len(complete) < 2:
            continue  # a single-outcome market can't be de-vigged
        devigged = [(b, devig_book_method(o, method)) for b, o in named_books if frozenset(o) == complete]
        kept = _drop_book_outliers([p for _b, p in devigged], complete, outlier_points)  # pass3#5
        # EV#2: a sharp/low-hold book (config sharp_books) counts `weight` times in the
        # weighted median; off when sharp_books is empty -> plain median (frozen behavior).
        per_outcome: dict[str, list[tuple[Decimal, int]]] = defaultdict(list)
        for book, book_probs in devigged:
            if book_probs not in kept:
                continue
            reps = weight if book in sharp_set else 1
            for outcome, prob in book_probs.items():
                per_outcome[outcome].append((prob, reps))
        # #3 totals are line-fragmented (books rarely share the exact line), so a 3-book
        # gate makes totals consensus nearly always dead. Use a lower totals threshold —
        # NOT half-point interpolation, which would change the event definition. Gate on the
        # count of distinct surviving books, not the (sharp-)weighted vote count.
        gate = totals_min if market_type == "TOTAL" else min_books
        if len(kept) < gate or any(o not in per_outcome for o in complete):
            continue
        medians = {o: _weighted_median(per_outcome[o]) for o in complete}
        total = sum(medians.values(), Decimal("0"))
        if total <= 0:
            continue
        for outcome, value in medians.items():  # #1: renormalize the median vector to 1
            consensus[outcome] = q6(value / total)
    return consensus


def _devig_cache(quotes: list[Quote], method: str) -> dict[tuple[str, str, Decimal | None], dict[str, Decimal]]:
    """D19: de-vig each (book, market_type, line) ONCE. consensus_probabilities and
    consensus_dispersion otherwise each re-iterate the quotes and re-run devig_book_method on
    the same books (and power/shin each run a bisection), so the scan paid for the de-vig twice.
    Filter/key are identical to both functions, so the cache is a drop-in shared input."""
    raw_books: dict[tuple[str, str, Decimal | None], dict[str, Decimal]] = defaultdict(dict)
    for quote in quotes:
        if quote.venue != "oddsapi" or quote.raw_price < Decimal("1.002") or quote.raw_price > Decimal("500"):
            continue
        key = consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label)
        raw_books[(quote.book, quote.market_type, quote.line)][key] = quote.raw_price
    return {k: devig_book_method(o, method) for k, o in raw_books.items()}


def consensus_all(quotes: list[Quote], min_books: int, min_books_totals: int | None = None,
                  method: str = "proportional", outlier_points: Decimal | None = None,
                  sharp_books: frozenset[str] | set[str] | None = None, sharp_weight: int = 2,
                  book_correlation: float = 0.0) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """D19: compute the consensus probabilities AND the cross-book dispersion from a SINGLE
    de-vig pass. Output is identical to calling consensus_probabilities and consensus_dispersion
    separately (verified in tests/test_consensus_bundle-style equivalence checks); this only
    removes the duplicated de-vig work in the scan hot path."""
    cache = _devig_cache(quotes, method)
    by_market_named: dict[tuple[str, Decimal | None], list[tuple[str, dict[str, Decimal]]]] = defaultdict(list)
    for (book, market_type, line), dv in cache.items():
        by_market_named[(market_type, line)].append((book, dv))

    sharp_set = frozenset(sharp_books) if sharp_books else frozenset()
    weight = max(1, int(sharp_weight))
    totals_min = min_books_totals if min_books_totals is not None else min_books
    rho = min(max(float(book_correlation), 0.0), 0.999)

    consensus: dict[str, Decimal] = {}
    dispersion: dict[str, Decimal] = {}
    for (market_type, _line), named_books in by_market_named.items():
        complete = _modal_complete_set([frozenset(dv) for _b, dv in named_books])
        if len(complete) < 2:
            continue
        devigged = [(b, dv) for b, dv in named_books if frozenset(dv) == complete]
        # --- probabilities (mirrors consensus_probabilities) ---
        kept = _drop_book_outliers([p for _b, p in devigged], complete, outlier_points)
        per_outcome: dict[str, list[tuple[Decimal, int]]] = defaultdict(list)
        for book, book_probs in devigged:
            if book_probs not in kept:
                continue
            reps = weight if book in sharp_set else 1
            for outcome, prob in book_probs.items():
                per_outcome[outcome].append((prob, reps))
        gate = totals_min if market_type == "TOTAL" else min_books
        if len(kept) >= gate and not any(o not in per_outcome for o in complete):
            medians = {o: _weighted_median(per_outcome[o]) for o in complete}
            total = sum(medians.values(), Decimal("0"))
            if total > 0:
                for outcome, value in medians.items():
                    consensus[outcome] = q6(value / total)
        # --- dispersion (mirrors consensus_dispersion; all complete books, no outlier drop) ---
        disp_per_outcome: dict[str, list[Decimal]] = defaultdict(list)
        for _book, dv in devigged:
            for outcome, prob in dv.items():
                disp_per_outcome[outcome].append(prob)
        for outcome, probs in disp_per_outcome.items():
            n = len(probs)
            if n >= max(2, min_books):
                n_eff = n / (1.0 + (n - 1) * rho) if rho > 0 else n
                se = stdev(float(p) for p in probs) / math.sqrt(n_eff)
                dispersion[outcome] = q6(Decimal(str(se)))
    return consensus, dispersion


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


def consensus_dispersion(quotes: list[Quote], min_books: int, method: str = "proportional",
                         book_correlation: float = 0.0) -> dict[str, Decimal]:
    """EV#3 / pass3#2: per-outcome cross-book STANDARD ERROR of the de-vigged probs as a
    confidence signal — wide book disagreement should widen e_min / shrink trust, not be
    silently swallowed by the median. Keyed like consensus_probabilities.

    EV#7: this is a true SE — `stdev(probs)/sqrt(n)` — not the max-min range. The range grows
    mechanically with the number of books, so it overstated dispersion exactly where more
    books should mean MORE confidence; the SE is what the edge/SE gate and the Kelly haircut
    actually want. Needs >= 2 surviving books to have a defined stdev.

    B6: books are NOT independent (many resell the same originating feed), so dividing the
    stdev by sqrt(n) understates the true SE and makes the edge/SE gate overconfident exactly
    when books cluster. `book_correlation` rho in [0,1) applies an effective-sample-size
    correction: n_eff = n / (1 + (n-1)*rho), and SE = stdev/sqrt(n_eff). rho=0 (default) is the
    legacy independent-book SE; rho=0.5 roughly halves the effective book count at large n."""
    rho = min(max(float(book_correlation), 0.0), 0.999)
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
        complete = _modal_complete_set([frozenset(o) for o in books])  # C14
        if len(complete) < 2:
            continue
        per_outcome: dict[str, list[Decimal]] = defaultdict(list)
        for odds in books:
            if frozenset(odds) == complete:
                for outcome, prob in devig_book_method(odds, method).items():
                    per_outcome[outcome].append(prob)
        for outcome, probs in per_outcome.items():
            n = len(probs)
            if n >= max(2, min_books):
                n_eff = n / (1.0 + (n - 1) * rho) if rho > 0 else n  # B6: effective sample size
                se = stdev(float(p) for p in probs) / math.sqrt(n_eff)
                dispersion[outcome] = q6(Decimal(str(se)))
    return dispersion


def consensus_outcome_times(quotes: list[Quote], min_books: int,
                            min_books_totals: int | None = None,
                            require_move_time: bool = False) -> dict[str, datetime]:
    """EV#6: per-outcome consensus AGE basis — the median `source_updated_at` (the book's own
    last line-move time) across the books that quote that outcome, falling back to a quote's
    `fetched_at` when a book can't report a move time. Stamping consensus with this instead of
    the scan wall-clock makes staleness/decay measure 'time since the line moved', not 'time
    since we last scanned'. Gated and keyed exactly like consensus_probabilities.

    C16: `source_updated_at` (line-move time) and `fetched_at` (our scan clock) are two
    different clocks; medianing them together biases the age. When `require_move_time` is True,
    only books that report a real move time contribute, so the age is a clean 'time since the
    line moved'; books without one are excluded (still subject to the per-outcome gate).
    Default False preserves the legacy fallback behavior."""
    raw_times: dict[tuple[str, str, Decimal | None], dict[str, datetime]] = defaultdict(dict)
    for quote in quotes:
        if quote.venue != "oddsapi" or quote.raw_price < Decimal("1.002") or quote.raw_price > Decimal("500"):
            continue
        ts = quote.source_updated_at if require_move_time else (quote.source_updated_at or quote.fetched_at)
        if ts is None:  # C16: no real move time and fallback disabled -> this book doesn't vote on age
            continue
        key = consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label)
        raw_times[(quote.book, quote.market_type, quote.line)][key] = ts
    by_market: dict[tuple[str, Decimal | None], list[dict[str, datetime]]] = defaultdict(list)
    for (_book, market_type, line), times in raw_times.items():
        by_market[(market_type, line)].append(times)
    totals_min = min_books_totals if min_books_totals is not None else min_books
    out: dict[str, datetime] = {}
    for (market_type, _line), books in by_market.items():
        complete = _modal_complete_set([frozenset(t) for t in books])  # C14
        if len(complete) < 2:
            continue
        per_outcome: dict[str, list[datetime]] = defaultdict(list)
        for times in books:
            if frozenset(times) == complete:
                for outcome, ts in times.items():
                    per_outcome[outcome].append(ts)
        gate = totals_min if market_type == "TOTAL" else min_books
        for outcome, tss in per_outcome.items():
            if len(tss) >= gate:
                epoch = median(t.timestamp() for t in tss)
                out[outcome] = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return out


def kalshi_fee_cost(price: Decimal, fee_rate: Decimal) -> Decimal:
    """Risk-based quadratic fee: rate·p·(1−p), peaks at p=0.5. Kalshi's curve — and, since the
    2026-03-30 schedule, Polymarket's too (fee = C·feeRate·p·(1−p), per-category feeRate). Both
    map to FEE_MODELS['quadratic']."""
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
    curve: 'quadratic' (p(1−p)) for Kalshi and Polymarket, 'linear' for SX."""
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


def shrink_probability(p_hat: Decimal, n_books: int, dispersion: Decimal = Decimal("0"),
                       prior: Decimal = Decimal("0.5"), prior_sd: float = 0.0,
                       prior_pseudocount: float = 0.0) -> Decimal:
    """B7: shrink a thin-consensus p_hat toward a prior. K7: the two regimes take two EXPLICIT
    knobs — the old single `prior_strength` meant a prior sd (prob units) in one regime and a
    pseudo-count of books in the other, ~100x apart, selected per-outcome at runtime.

      * prior_sd > 0 and dispersion > 0 -> normal-normal posterior mean. The per-book SE
        (`dispersion`) is the sampling sd of p_hat; `prior_sd` is tau. Posterior mean =
        (p/se^2 + prior/tau^2)/(1/se^2 + 1/tau^2).
      * prior_pseudocount > 0 -> count shrinkage: w = k/(k + n_books); p' = p + w*(prior - p).

    Both zero (default) returns p_hat unchanged; config load errors if both are set."""
    p = float(p_hat)
    pr = float(prior)
    se = float(dispersion)
    if prior_sd > 0 and se > 0:
        tau2 = prior_sd * prior_sd
        post = (p / (se * se) + pr / tau2) / (1.0 / (se * se) + 1.0 / tau2)
        return q6(Decimal(str(post)))
    if prior_pseudocount > 0:
        w = prior_pseudocount / (prior_pseudocount + max(0, int(n_books)))
        return q6(Decimal(str(p + w * (pr - p))))
    return p_hat


def push_probability_poisson(line: Decimal, lam: float | None = None) -> Decimal:
    """B11: probability an integer TOTAL lands exactly ON the line (a PUSH — stake returned, no
    payout). Modeled as total runs ~ Poisson(lambda); the fair lambda is unknown so it defaults
    to the line itself (the market's central expectation). Returns 0 for non-integer lines
    (they can't push). First-order: a Poisson is a crude run-total model, but any non-zero push
    mass is better than pricing an integer total as a clean binary."""
    if line is None or line != line.to_integral_value():
        return Decimal("0")
    L = int(line)
    if L < 0:
        return Decimal("0")
    mu = float(line) if lam is None else float(lam)
    if mu <= 0:
        return Decimal("0")
    pmf = math.exp(-mu + L * math.log(mu) - math.lgamma(L + 1))  # Poisson pmf P(X = L)
    return q6(Decimal(str(pmf)))


def push_adjusted_edge(edge: Decimal, push_prob: Decimal) -> Decimal:
    """B11: scale an edge by (1 - push_prob). On a push the contract returns stake (net 0), so
    the mass the binary model split into win/lose at the de-vigged rate earns nothing; to
    first order that removes a fraction `push_prob` of the expected edge. push_prob=0 is a
    no-op, so non-integer totals and disabled push-pricing are unchanged."""
    u = min(max(push_prob, Decimal("0")), Decimal("1"))
    return q6(edge * (Decimal("1") - u))


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
