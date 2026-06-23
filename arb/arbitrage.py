from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from .capacity import max_economic_arb_size_quotes
from .models import Event, Opportunity, Quote
from .pricing import is_economic, money, q6


# Venues whose legs can be re-quoted to a verifiable cost. SX legs now carry an outcome-
# side hint (Quote.ref) so requote_sx_leg can re-price them, so SX is verifiable too.
# (Legs recorded before ref existed have no hint and still grade UNVERIFIABLE.)
VERIFIABLE_VENUES = {"kalshi", "polymarket", "oddsapi", "sx"}


def has_unverifiable_leg(legs: list[Quote]) -> bool:
    return any(leg.venue not in VERIFIABLE_VENUES for leg in legs)


def capturable_lower_bound(legs: list[Quote], roi: Decimal) -> Decimal:
    """Lower bound on dollars of locked profit: min top-of-book dollar-notional depth across
    legs × ROI. Each adapter reports depth_usd as $-notional at the ask, so the min is the
    binding leg. If ANY leg has unknown depth the bound is unknowable -> 0 (don't overstate
    it by silently dropping that leg). Top-of-book only — books refill, so it's a floor."""
    depths = [leg.depth_usd for leg in legs]
    if not depths or any(d is None for d in depths):
        return Decimal("0")
    return q6(min(depths) * roi)


def leg_skew_secs(legs: list[Quote]) -> float:
    """Seconds between the oldest and newest leg observation — legs fetched far apart
    were never simultaneously available, so the 'edge' may span two moments in time."""
    times = [leg.fetched_at for leg in legs]
    return (max(times) - min(times)).total_seconds()


def start_drift_secs(legs: list[Quote]) -> float:
    """Disagreement between venues on WHEN the event starts. Venues listing the same
    game agree to the minute; a large drift means the match glued two different games."""
    starts = [leg.start_utc for leg in legs if leg.start_utc is not None]
    if len(starts) < 2:
        return 0.0
    return (max(starts) - min(starts)).total_seconds()


def same_outcome_spread(quotes: list[Quote]) -> Decimal:
    """Largest cross-venue cost gap for any single outcome in the group. A same-outcome
    gap of 20+ points is almost always a matching error, not a real price."""
    by_outcome: dict[str, list[Decimal]] = defaultdict(list)
    for quote in quotes:
        by_outcome[quote.outcome_label].append(quote.effective_cost)
    spreads = [max(costs) - min(costs) for costs in by_outcome.values() if len(costs) > 1]
    return max(spreads, default=Decimal("0"))


def venue_pair_key(legs: list[Quote]) -> str:
    venues = sorted({leg.venue for leg in legs})
    if len(venues) == 1:
        return f"{venues[0]}+{venues[0]}"
    return "+".join(venues)


def status_for_legs(legs: list[Quote], trusted_pairs: set[str]) -> tuple[str, tuple[str, ...]]:
    notes: list[str] = []
    status = "OK"
    pair = venue_pair_key(legs)
    sport_pair = f"{pair}:{legs[0].sport}" if legs else pair
    whitelisted = sport_pair in trusted_pairs or pair in trusted_pairs
    cross_venue = len({leg.venue for leg in legs}) > 1
    # SPEC §3.3: SX void rules differ from Kalshi/Polymarket binaries, so adapters tag those
    # legs settlement-rule-review; they stay REVIEW even single-venue until whitelisted.
    flagged_leg = any("settlement-rule-review" in leg.notes for leg in legs)
    if (cross_venue or flagged_leg) and not whitelisted:
        status = "REVIEW"
        notes.append("settlement-rule-review")
    if any("fuzzy-match" in leg.notes for leg in legs):
        status = "REVIEW"
        notes.append("fuzzy-match")
    return status, tuple(notes)


def build_event(event_key: str, quotes: list[Quote]) -> Event:
    first = quotes[0]
    start = first.start_utc or datetime.now(timezone.utc)
    return Event(
        event_key=event_key,
        category="sports",
        sport=first.sport,
        home=first.home or "",
        away=first.away or "",
        start_utc=start,
    )


def detect_true_arbs(
    quotes: list[Quote],
    bankroll: Decimal,
    max_stake_pct: Decimal,
    trusted_pairs: set[str],
    stale_warn: Decimal,
    max_leg_skew_secs: int | None = None,
    max_start_drift_mins: int | None = None,
    match_suspect_prob_diff: Decimal | None = None,
    capital_charge_annual: Decimal | None = None,
    size_by_ladder: bool = False,
) -> list[Opportunity]:
    # An arb must cover the outcomes of ONE market: group per event + market type + exact
    # line so team winners never blend with OVER/UNDER or with a different total line.
    by_market: dict[tuple[str, str, Decimal | None], list[Quote]] = defaultdict(list)
    for quote in quotes:
        if quote.venue == "oddsapi":
            continue  # The Odds API is consensus-only; you cannot bet there, so it is
                      # never an arb leg (this also removes the dead _arb_cost oddsapi path).
        by_market[(quote.event_key, quote.market_type, quote.line)].append(quote)

    opportunities: list[Opportunity] = []
    budget = bankroll * max_stake_pct
    for (event_key, _market_type, _line), event_quotes in by_market.items():
        by_outcome: dict[str, list[Quote]] = defaultdict(list)
        for quote in event_quotes:
            by_outcome[quote.outcome_label].append(quote)
        if len(by_outcome) < 2:
            continue
        # Soccer winners are three-way; HOME+AWAY without DRAW does not cover the event.
        if _market_type == "WINNER" and event_quotes[0].sport == "soccer" and len(by_outcome) < 3:
            continue
        best_legs = [min(outcome_quotes, key=_arb_cost) for outcome_quotes in by_outcome.values()]
        total_cost = sum((_arb_cost(leg) for leg in best_legs), Decimal("0"))
        if total_cost >= Decimal("1"):
            continue
        # Match sanity gates: a suspect match is suspended, never flagged as an ARB —
        # a "20% arb" between venues that disagree on the game is a matching error.
        if max_start_drift_mins is not None and start_drift_secs(best_legs) > max_start_drift_mins * 60:
            continue
        if match_suspect_prob_diff is not None and same_outcome_spread(event_quotes) > match_suspect_prob_diff:
            continue
        roi = q6(Decimal("1") / total_cost - Decimal("1"))
        # Microstructure capacity from the depth ladders: break-even size (where the lock goes
        # negative), profit-max size, and whether edge or depth is the binding limit.
        cap = max_economic_arb_size_quotes(best_legs)
        arb_budget = budget
        if size_by_ladder and cap.feasible and cap.profitmax_stake_usd > 0:
            arb_budget = money(min(cap.profitmax_stake_usd * Decimal("0.5"), budget))
        stakes, guaranteed_floor = equalized_arb_stakes(best_legs, arb_budget, total_cost)
        status, notes = status_for_legs(best_legs, trusted_pairs)
        # #8 report the REAL guaranteed worst-case profit after cent-rounding, not the
        # idealized ROI. If rounding pushed the worst leg negative the lock didn't hold.
        notes = tuple([*notes, f"floor:{guaranteed_floor}"])
        if guaranteed_floor < 0:
            status = "REVIEW"
            notes = tuple([*notes, "rounding-broke-lock"])
        if roi > stale_warn:
            status = "REVIEW"
            notes = tuple([*notes, "likely-stale-quote"])
        if max_leg_skew_secs is not None and leg_skew_secs(best_legs) > max_leg_skew_secs:
            status = "REVIEW"
            notes = tuple([*notes, f"time-skew:{int(leg_skew_secs(best_legs))}s"])
        # #6 capacity lower bound (top-of-book; books refill, so this is a floor).
        notes = tuple([*notes, f"capturable:{capturable_lower_bound(best_legs, roi)}"])
        # Ladder capacity: $ you can stake before the lock goes negative (break-even), the
        # profit-max size, and whether edge or depth binds. Measurement; needs captured ladders.
        if cap.feasible:
            notes = tuple([*notes, f"capacity:be={cap.breakeven_stake_usd},"
                           f"max={cap.profitmax_stake_usd}@{cap.max_profit_usd},by={cap.limited_by}"])
        # Capital charge: a tiny lock held to a far settlement may not beat cash. Tag it
        # 'uneconomic' (still recorded) so the daily report can separate real from trivial.
        if capital_charge_annual is not None:
            start = best_legs[0].start_utc
            days = ((start - datetime.now(timezone.utc)).total_seconds() / 86400.0) if start else 0.0
            if not is_economic(roi, days, capital_charge_annual):
                notes = tuple([*notes, "uneconomic"])
        # #1 an arb with any unverifiable leg can never be confirmed → RESEARCH:
        # logged for the archive, but downstream never bets it or assigns a hot thread.
        if has_unverifiable_leg(best_legs):
            status = "REVIEW"
            notes = tuple([*notes, "research-unverifiable"])
        opportunities.append(
            Opportunity(
                kind="ARB",
                category="sports",
                event=build_event(event_key, event_quotes),
                legs=best_legs,
                edge=roi,
                edge_points=q6(Decimal("1") - total_cost),  # margin below $1, comparable
                stakes=stakes,
                status=status,  # type: ignore[arg-type]
                notes=notes,
            )
        )
    return opportunities


def detect_divergences(
    quotes: list[Quote],
    threshold: Decimal,
    trusted_pairs: set[str],
    max_leg_skew_secs: int | None = None,
    max_start_drift_mins: int | None = None,
    match_suspect_prob_diff: Decimal | None = None,
) -> list[Opportunity]:
    grouped: dict[tuple[str, str, Decimal | None, str], list[Quote]] = defaultdict(list)
    for quote in quotes:
        if quote.venue == "oddsapi":
            continue
        grouped[(quote.event_key, quote.market_type, quote.line, quote.outcome_label)].append(quote)

    opportunities: list[Opportunity] = []
    for (event_key, _market_type, _line, _outcome), outcome_quotes in grouped.items():
        venues = {quote.venue for quote in outcome_quotes}
        if len(venues) < 2:
            continue
        cheapest = min(outcome_quotes, key=lambda quote: quote.effective_cost)
        priciest = max(outcome_quotes, key=lambda quote: quote.effective_cost)
        spread = q6(priciest.effective_cost - cheapest.effective_cost)
        if abs(spread) < threshold:
            continue
        status, notes = status_for_legs([cheapest, priciest], trusted_pairs)
        if max_leg_skew_secs is not None and leg_skew_secs([cheapest, priciest]) > max_leg_skew_secs:
            status = "REVIEW"
            notes = tuple([*notes, f"time-skew:{int(leg_skew_secs([cheapest, priciest]))}s"])
        # Divergences ARE the record of big spreads, so suspect ones stay recorded —
        # but tagged so nothing downstream mistakes a probable mismatch for a price.
        if match_suspect_prob_diff is not None and spread > match_suspect_prob_diff:
            status = "REVIEW"
            notes = tuple([*notes, "match-suspect"])
        if max_start_drift_mins is not None and start_drift_secs([cheapest, priciest]) > max_start_drift_mins * 60:
            status = "REVIEW"
            notes = tuple([*notes, "match-suspect-start-drift"])
        event_quotes = [quote for quote in quotes if quote.event_key == event_key]
        opportunities.append(
            Opportunity(
                kind="DIVERGENCE",
                category="sports",
                event=build_event(event_key, event_quotes or outcome_quotes),
                legs=[cheapest, priciest],
                edge=spread,
                stakes={cheapest.outcome_label: Decimal("0.00")},
                status=status,  # type: ignore[arg-type]
                notes=tuple([*notes, "spread-only"]),
            )
        )
    return opportunities


def split_match_suspects(opportunities: list[Opportunity]) -> tuple[list[Opportunity], list[Opportunity]]:
    """Probable matching errors must not pollute the divergence research dataset —
    they are quarantined for human classification instead of recorded as prices."""
    clean: list[Opportunity] = []
    suspects: list[Opportunity] = []
    for opp in opportunities:
        if any(note.startswith("match-suspect") for note in opp.notes):
            suspects.append(opp)
        else:
            clean.append(opp)
    return clean, suspects


def equalized_arb_stakes(legs: list[Quote], budget: Decimal, total_cost: Decimal) -> tuple[dict[str, Decimal], Decimal]:
    """Stake each leg proportional to its cost (which equalizes per-outcome payout), round
    to cents, then return the REAL guaranteed floor = min over outcomes of (that leg's
    payout − total staked). Per-leg cent-rounding makes payouts slightly unequal, so the
    locked profit is this worst-case floor, not budget·ROI — and it can go negative on a
    small bankroll or many legs, which the caller flags."""
    stakes = {leg.outcome_label: money(budget * _arb_cost(leg) / total_cost) for leg in legs}
    total_staked = sum(stakes.values(), Decimal("0"))
    # If outcome i wins, you collect stake_i / cost_i (each $1-payout contract).
    payouts = [stakes[leg.outcome_label] / _arb_cost(leg) for leg in legs if _arb_cost(leg) > 0]
    guaranteed_floor = money(min(payouts) - total_staked) if payouts else Decimal("0.00")
    return stakes, guaranteed_floor


def _arb_cost(quote: Quote) -> Decimal:
    # oddsapi quotes never reach here (filtered in detection — consensus-only, not tradeable).
    return quote.effective_cost
