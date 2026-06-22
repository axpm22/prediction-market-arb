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
import random
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


def lead_lag(store: Store, move_threshold: float = 0.01, max_lag_secs: float = 300.0,
             null_shuffles: int = 20, null_correct_margin: float = 5.0) -> dict:
    rows = _rows(store)
    # group cost series per (event,outcome,line) and venue
    series: dict[tuple, dict[str, list[tuple[datetime, float]]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r["event_key"], r["outcome"], r["market_type"], r["line"])
        series[key][r["venue"]].append((datetime.fromisoformat(r["fetched_at"]), float(r["eff"])))

    pair_leads: dict[tuple[str, str], list[float]] = defaultdict(list)
    pair_null: dict[tuple[str, str], list[float]] = defaultdict(list)
    move_counts: dict[str, int] = defaultdict(int)
    rng = random.Random(20260622)  # deterministic null baseline
    for venues in series.values():
        moves_by_venue = {v: _moves(s, move_threshold) for v, s in venues.items() if len(s) > 1}
        for v, ms in moves_by_venue.items():
            move_counts[v] += len(ms)
        present = [v for v in moves_by_venue if moves_by_venue[v]]
        for a in present:
            for b in present:
                if a == b:
                    continue
                pair_leads[(a, b)].extend(_following_leads(moves_by_venue[a], moves_by_venue[b], max_lag_secs))
                # Null: keep B's move COUNT and time span but randomize when they happen.
                # A high-density B yields small "leads" by base rate alone; the observed
                # lead only means something if it beats this null.
                if null_shuffles:
                    pair_null[(a, b)].extend(
                        _null_leads(moves_by_venue[a], moves_by_venue[b], max_lag_secs, null_shuffles, rng)
                    )

    pairs = {}
    for (a, b), leads in sorted(pair_leads.items()):
        if len(leads) < 5:
            continue
        obs = round(median(leads), 1)
        null = pair_null.get((a, b))
        null_med = round(median(null), 1) if null else None
        # A real lead is meaningfully FASTER than the density-matched null.
        corrected = round(obs - null_med, 1) if null_med is not None else None
        pairs[f"{a}->{b}"] = {
            "n": len(leads),
            "median_lead_secs": obs,
            "null_median_lead_secs": null_med,
            "corrected_lead_secs": corrected,
            "real_signal": bool(corrected is not None and corrected < -float(null_correct_margin)),
        }
    return {
        "move_threshold": move_threshold,
        "max_lag_secs": max_lag_secs,
        "moves_per_venue": dict(move_counts),
        "pairs": pairs,
    }


def _following_leads(a_moves, b_moves, max_lag_secs: float) -> list[float]:
    leads = []
    for ta, da in a_moves:
        following = [(tb - ta).total_seconds() for tb, db in b_moves
                     if db == da and 0 < (tb - ta).total_seconds() <= max_lag_secs]
        if following:
            leads.append(min(following))
    return leads


def _null_leads(a_moves, b_moves, max_lag_secs: float, shuffles: int, rng) -> list[float]:
    """Lead distribution if B's moves were randomly placed in B's own time span (same count,
    same directions, no causal link to A) — the density-matched baseline."""
    if not b_moves:
        return []
    times = [t for t, _ in b_moves]
    lo, hi = min(times).timestamp(), max(times).timestamp()
    dirs = [d for _, d in b_moves]
    out: list[float] = []
    for _ in range(shuffles):
        shuffled = [(datetime.fromtimestamp(rng.uniform(lo, hi), tz=times[0].tzinfo), d) for d in dirs]
        out.extend(_following_leads(a_moves, shuffled, max_lag_secs))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arb.experimental.analysis.leadlag")
    parser.add_argument("--move-threshold", type=float, default=0.01)
    parser.add_argument("--max-lag-secs", type=float, default=300.0)
    args = parser.parse_args(argv)
    store = Store(load_config().database_path)
    summary = lead_lag(store, args.move_threshold, args.max_lag_secs)
    print(f"Moves per venue (>= {summary['move_threshold']} step): {summary['moves_per_venue']}")
    if not summary["pairs"]:
        print("No venue pair has >= 5 co-moves yet; archive needs more overlapping observation.")
        return 0
    print("Ordered lead times vs density-matched null (A moves first, B follows; n >= 5):")
    print(f"  {'PAIR':28s} {'OBS':>7} {'NULL':>7} {'CORR':>7}  REAL?")
    for pair, s in sorted(summary["pairs"].items(), key=lambda kv: kv[1]["median_lead_secs"]):
        print(f"  {pair:28s} {s['median_lead_secs']:6.1f}s {s['null_median_lead_secs'] or 0:6.1f}s "
              f"{s['corrected_lead_secs'] or 0:6.1f}s  {'YES' if s['real_signal'] else 'no (density artifact)'}  (n={s['n']})")
    print("\nOBS = observed median lead; NULL = median lead if B's moves were random at the same")
    print("density; CORR = OBS - NULL. Only 'REAL? YES' (OBS meaningfully below NULL) is a true lead.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
