"""weakness#10 — DIVERGENCE mean-reversion (BACKLOG_ROADMAP Wave 0, offline & read-only).

Divergences are recorded with stake=0 and never scored, so the "stat-arb fuel" never yields a
mean-reversion result. This turns the persisted quote archive into that result: for each
(event, outcome) it builds the cross-venue price spread per poll (watch polls fetch all venues
together, so poll_id is a clean time index), then asks — when the spread is LARGE, does it
narrow on the next poll more often than a baseline? If large spreads don't revert, a
divergence is a matching error, not signal. Nothing here writes to the DB.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean
from typing import Any

from arb.pricing import consensus_outcome_key
from arb.store import Store

TRADEABLE = ("kalshi", "polymarket", "sx")


@dataclass(frozen=True)
class ReversionStats:
    large_threshold: float
    n_large: int
    n_narrowed_next: int        # of the large spreads, how many shrank on the next poll
    narrow_rate: float
    mean_delta_large: float     # mean (next - current) spread on large spreads (<0 = reverts)
    n_baseline: int
    baseline_narrow_rate: float  # narrow rate over ALL consecutive pairs (the null)


def reversion_stats(series_by_key: dict[tuple[str, str], list[tuple[int, float]]],
                    large_threshold: float) -> ReversionStats:
    """Pure core. series_by_key: (event, outcome) -> [(poll_id, spread)] (any order)."""
    large_deltas: list[float] = []
    large_narrowed = 0
    base_pairs = 0
    base_narrowed = 0
    for series in series_by_key.values():
        ordered = sorted(series)
        for (_, cur), (_, nxt) in zip(ordered, ordered[1:]):
            base_pairs += 1
            if nxt < cur:
                base_narrowed += 1
            if cur >= large_threshold:
                large_deltas.append(nxt - cur)
                if nxt < cur:
                    large_narrowed += 1
    n_large = len(large_deltas)
    return ReversionStats(
        large_threshold=large_threshold,
        n_large=n_large,
        n_narrowed_next=large_narrowed,
        narrow_rate=round(large_narrowed / n_large, 4) if n_large else 0.0,
        mean_delta_large=round(fmean(large_deltas), 6) if large_deltas else 0.0,
        n_baseline=base_pairs,
        baseline_narrow_rate=round(base_narrowed / base_pairs, 4) if base_pairs else 0.0,
    )


def spread_series(store: Store) -> dict[tuple[str, str], list[tuple[int, float]]]:
    """Per (event, outcome): the cross-venue (max-min) cost spread within each poll that had
    >=2 tradeable venues quoting it."""
    with store.connect() as con:
        rows = con.execute(
            f"""SELECT poll_id, event_key, venue, market_type, line, outcome, cost
                  FROM quotes
                 WHERE venue IN ({','.join('?' * len(TRADEABLE))}) AND poll_id IS NOT NULL""",
            TRADEABLE,
        ).fetchall()
    # (event, outcome, poll) -> {venue: cost}
    by_cell: dict[tuple[str, str, int], dict[str, Decimal]] = {}
    for r in rows:
        try:
            cost = Decimal(str(r["cost"]))
        except (TypeError, ValueError):
            continue
        outcome = consensus_outcome_key(str(r["market_type"]), None if r["line"] in (None, "") else Decimal(str(r["line"])), str(r["outcome"]))
        by_cell.setdefault((r["event_key"], outcome, int(r["poll_id"])), {})[str(r["venue"])] = cost
    series: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for (event_key, outcome, poll_id), venue_costs in by_cell.items():
        if len(venue_costs) < 2:
            continue
        spread = float(max(venue_costs.values()) - min(venue_costs.values()))
        series.setdefault((event_key, outcome), []).append((poll_id, spread))
    return {k: v for k, v in series.items() if len(v) >= 2}


def run_reversion(store: Store, large_threshold: float = 0.03) -> ReversionStats:
    return reversion_stats(spread_series(store), large_threshold)


def format_reversion(s: ReversionStats) -> str:
    verdict = ("reverts faster than baseline" if s.narrow_rate > s.baseline_narrow_rate + 0.05
               else "no reversion edge vs baseline (likely matching noise)")
    return "\n".join([
        f"DIVERGENCE mean-reversion (cross-venue spread, next-poll horizon)",
        f"  large spreads (>= {s.large_threshold}): {s.n_large}",
        f"  narrowed next poll: {s.n_narrowed_next} ({s.narrow_rate:.1%})",
        f"  mean delta on large spreads: {s.mean_delta_large:+.6f} (negative = reverts)",
        f"  baseline narrow rate (all pairs, n={s.n_baseline}): {s.baseline_narrow_rate:.1%}",
        f"  verdict: {verdict}",
    ])
