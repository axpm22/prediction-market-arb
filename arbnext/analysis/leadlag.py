"""Lead-lag analysis over the quote archive (review #9) — pure offline, zero live risk.

When a game's fair price moves, which venue moves FIRST, and by how long? If one venue
systematically leads another, you stop hunting arbs at random and start predicting where
the lagging venue will be mispriced the instant the leader moves. This reads the existing
`quotes` table only — nothing is fetched, nothing is written.

Method, deliberately simple and robust:
  - Per (event, outcome), build each venue's time series of effective cost.
  - Detect a "move" = |Δcost| >= move_threshold between consecutive same-venue quotes.
  - For each move on venue A, find the nearest following move (same direction) on venue B
    within max_lag; record the lead time. Aggregate median lead per ordered venue pair.
A positive median lead for A→B means A tends to move first; B is the one to quote fast.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from statistics import median

from arb.config import load_config
from arb.store import Store


def _rows(store: Store):
    with store.connect() as con:
        return list(
            con.execute(
                """
                SELECT event_key, venue, outcome, market_type, line, fetched_at,
                       CAST(cost AS REAL) + CAST(fee_cost AS REAL) AS eff
                  FROM quotes
                 WHERE venue != 'oddsapi'
                 ORDER BY event_key, outcome, venue, fetched_at
                """
            )
        )


def _moves(series: list[tuple[datetime, float]], threshold: float) -> list[tuple[datetime, int]]:
    """Return (time, direction) for each significant step; direction in {-1,+1}."""
    moves = []
    for (t0, p0), (t1, p1) in zip(series, series[1:]):
        delta = p1 - p0
        if abs(delta) >= threshold:
            moves.append((t1, 1 if delta > 0 else -1))
    return moves


def lead_lag(store: Store, move_threshold: float = 0.01, max_lag_secs: float = 300.0) -> dict:
    rows = _rows(store)
    # group cost series per (event,outcome,line) and venue
    series: dict[tuple, dict[str, list[tuple[datetime, float]]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r["event_key"], r["outcome"], r["market_type"], r["line"])
        series[key][r["venue"]].append((datetime.fromisoformat(r["fetched_at"]), float(r["eff"])))

    pair_leads: dict[tuple[str, str], list[float]] = defaultdict(list)
    move_counts: dict[str, int] = defaultdict(int)
    for venues in series.values():
        moves_by_venue = {v: _moves(s, move_threshold) for v, s in venues.items() if len(s) > 1}
        for v, ms in moves_by_venue.items():
            move_counts[v] += len(ms)
        present = [v for v in moves_by_venue if moves_by_venue[v]]
        for a in present:
            for b in present:
                if a == b:
                    continue
                for ta, da in moves_by_venue[a]:
                    # nearest same-direction move on b strictly after ta, within max_lag
                    following = [
                        (tb - ta).total_seconds()
                        for tb, db in moves_by_venue[b]
                        if db == da and 0 < (tb - ta).total_seconds() <= max_lag_secs
                    ]
                    if following:
                        pair_leads[(a, b)].append(min(following))

    summary = {
        "move_threshold": move_threshold,
        "max_lag_secs": max_lag_secs,
        "moves_per_venue": dict(move_counts),
        "pairs": {
            f"{a}->{b}": {
                "n": len(leads),
                "median_lead_secs": round(median(leads), 1),
                "mean_lead_secs": round(sum(leads) / len(leads), 1),
            }
            for (a, b), leads in sorted(pair_leads.items())
            if len(leads) >= 5
        },
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arbnext.analysis.leadlag")
    parser.add_argument("--move-threshold", type=float, default=0.01)
    parser.add_argument("--max-lag-secs", type=float, default=300.0)
    args = parser.parse_args(argv)
    store = Store(load_config().database_path)
    summary = lead_lag(store, args.move_threshold, args.max_lag_secs)
    print(f"Moves per venue (>= {summary['move_threshold']} step): {summary['moves_per_venue']}")
    if not summary["pairs"]:
        print("No venue pair has >= 5 co-moves yet; archive needs more overlapping observation.")
        return 0
    print("Ordered lead times (A moves first, B follows; n >= 5):")
    for pair, stats in sorted(summary["pairs"].items(), key=lambda kv: kv[1]["median_lead_secs"]):
        print(f"  {pair:28s} median {stats['median_lead_secs']:6.1f}s  mean {stats['mean_lead_secs']:6.1f}s  (n={stats['n']})")
    print("\nInterpretation: a small median lead for A->B means A tends to move first — quote B fast when A moves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
