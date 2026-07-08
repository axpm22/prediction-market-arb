"""Lead–lag study (PROMPT_LIVE_LEADLAG Part B): pure read-time analysis over
stored quotes. Primary: first-mover tally with cluster-bootstrap CI over
event_key. Secondary: cross-correlation on a poll grid. Also emits observed
live line velocities (B4) to replace the config PLACEHOLDERS by dated change.
Results append to leadlag_runs (B5) — reports read stored runs, never
recompute silently with different params."""
from __future__ import annotations

import json
import random
import statistics
from datetime import datetime, timezone
from decimal import Decimal

from arb.matching import STRICT_BINARY_WINNER_SPORTS, line_key
from arb.models import q6


def _mapped_key(r: dict) -> tuple | None:
    """Complement-map BEFORE keying — same legality as detection (B1):
    soccer NO and integer-line NO never map."""
    outcome, side = r["outcome"], r["side"]
    if side == "NO":
        if r["market_type"] == "WINNER" and r["sport"] in STRICT_BINARY_WINNER_SPORTS:
            names = r["event_key"].split(":")[2:4]
            if len(names) == 2 and outcome in names:
                outcome = names[1] if outcome == names[0] else names[0]
                side = "YES"
        elif r["market_type"] == "TOTAL":
            lk = line_key(r["line"])
            if lk and "." in lk:
                outcome, side = ("UNDER" if outcome == "OVER" else "OVER"), "YES"
    return (r["event_key"], r["sport"], r["market_type"], line_key(r["line"]),
            outcome, side)


def build_series(store) -> tuple[dict, float]:
    """(series[(prop..., venue)] = [(t, mid)], excluded_no_bid_fraction)."""
    rows = store.query(
        """SELECT event_key, sport, venue, market_type, line, outcome, side,
                  fetched_at, cost, bid FROM quotes
           WHERE event_key != '' ORDER BY fetched_at""")
    series: dict[tuple, list] = {}
    no_bid = 0
    for r in rows:
        if r["bid"] is None:
            no_bid += 1
            continue
        key = _mapped_key(dict(r)) + (r["venue"],)
        mid = (Decimal(r["cost"]) + Decimal(r["bid"])) / 2
        series.setdefault(key, []).append(
            (datetime.fromisoformat(r["fetched_at"]), mid))
    return series, (no_bid / len(rows) if rows else 0.0)


def moves(points: list, min_move: Decimal) -> list[tuple]:
    """[(t, sign, |delta|)] for |Δmid| >= min_move between consecutive obs."""
    out = []
    for (t0, m0), (t1, m1) in zip(points, points[1:]):
        d = m1 - m0
        if abs(d) >= min_move:
            out.append((t1, 1 if d > 0 else -1, abs(d)))
    return out


def _match(mv_a: list, mv_b: list, window: float, tie_eps: float) -> list[tuple]:
    """Matched same-sign move pairs -> (winner 0|1|None, lead_secs). A |Δt| within
    tie_eps is a TIE: inside one poll cycle the fetch ORDER stamps the earlier
    time (kalshi ~0.7s vs PM ~15s), not market leadership — dated refinement
    2026-07-09, pre-gate."""
    out, used_b = [], set()
    for t_a, sign_a, _ in mv_a:
        best = None
        for j, (t_b, sign_b, _) in enumerate(mv_b):
            if j in used_b or sign_b != sign_a:
                continue
            dt = (t_b - t_a).total_seconds()
            if abs(dt) <= window and (best is None or abs(dt) < abs(best[1])):
                best = (j, dt)
        if best is None:
            continue
        used_b.add(best[0])
        dt = best[1]
        winner = None if abs(dt) <= tie_eps else 0 if dt > 0 else 1
        out.append((winner, abs(dt)))
    return out


def cross_correlation(a: list, b: list, max_lag: int) -> tuple[int, Decimal]:
    """argmax-lag cross-correlation of change series on a common grid (B3).
    Positive lag k = series A leads B by k grid steps. Floats internally
    (rule 5 root-finding class), re-quantized on return."""
    fa, fb = [float(x) for x in a], [float(x) for x in b]

    def corr(k):
        pairs = [(fa[i], fb[i + k]) for i in range(len(fa))
                 if 0 <= i + k < len(fb)]
        if len(pairs) < 2:
            return 0.0
        xs, ys = zip(*pairs)
        sx = (sum(x * x for x in xs)) ** 0.5
        sy = (sum(y * y for y in ys)) ** 0.5
        return sum(x * y for x, y in pairs) / (sx * sy) if sx and sy else 0.0

    best = max(range(-max_lag, max_lag + 1), key=corr)
    return best, q6(Decimal(corr(best)))


def _bootstrap_ci(by_event: dict, seed: int, n: int = 1000) -> tuple[float, float]:
    """Cluster bootstrap over event_key — the project's standard CI (B2)."""
    rng = random.Random(seed)
    events = list(by_event)
    ps = []
    for _ in range(n):
        w0 = w1 = 0
        for ek in (rng.choice(events) for _ in events):
            a, b = by_event[ek]
            w0 += a
            w1 += b
        if w0 + w1:
            ps.append(w0 / (w0 + w1))
    ps.sort()
    return (ps[int(0.025 * len(ps))], ps[int(0.975 * len(ps))]) if ps else (0.0, 1.0)


def run_study(cfg, store, seed: int = 1729) -> dict:
    ll = cfg["leadlag"]
    series, excluded = build_series(store)
    window = float(ll["match_window_secs"])
    min_move = Decimal(str(ll["min_move_points"]))

    by_prop: dict[tuple, dict] = {}
    for key, points in series.items():
        by_prop.setdefault(key[:-1], {})[key[-1]] = points

    pairs: dict[str, dict] = {}
    for prop, venues in by_prop.items():
        names = sorted(venues)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                va, vb = names[i], names[j]
                matched = _match(moves(venues[va], min_move),
                                 moves(venues[vb], min_move), window,
                                 float(ll["tie_epsilon_secs"]))
                if not matched:
                    continue
                p = pairs.setdefault(f"{va}:{vb}", {
                    "by_event": {}, "leads": [], "n_ties": 0})
                ev = p["by_event"].setdefault(prop[0], [0, 0])
                for winner, lead in matched:
                    if winner is None:
                        p["n_ties"] += 1
                        continue
                    ev[winner] += 1
                    p["leads"].append((winner, lead))

    results = {}
    for pair, p in pairs.items():
        w0 = sum(a for a, _ in p["by_event"].values())
        w1 = sum(b for _, b in p["by_event"].values())
        if not (w0 + w1):
            continue
        p_first = w0 / (w0 + w1)
        lo, hi = _bootstrap_ci(p["by_event"], seed)
        first, second = pair.split(":")
        leader = first if lo > 0.5 else second if hi < 0.5 else None
        win_idx = 0 if leader == first else 1 if leader == second else None
        lead_pool = [s for w, s in p["leads"] if win_idx is None or w == win_idx]
        results[pair] = {
            "n_matched": w0 + w1 + p["n_ties"], "n_ties": p["n_ties"],
            "p_lead_first": round(p_first, 6), "ci_low": round(lo, 6),
            "ci_high": round(hi, 6), "leader": leader,
            "median_lead_secs": statistics.median(lead_pool) if lead_pool else None,
            "n_events": len(p["by_event"]),
        }

    # B4: observed live line velocity per sport x venue (median |Δmid|/Δt)
    velocities: dict[str, dict] = {}
    rates: dict[tuple, list] = {}
    live_rows = store.query(
        """SELECT event_key, sport, venue, market_type, line, outcome, side,
                  fetched_at, cost, bid FROM quotes
           WHERE live = 1 AND bid IS NOT NULL AND event_key != ''
           ORDER BY fetched_at""")
    lseries: dict[tuple, list] = {}
    for r in live_rows:
        key = _mapped_key(dict(r)) + (r["venue"],)
        mid = (Decimal(r["cost"]) + Decimal(r["bid"])) / 2
        lseries.setdefault(key, []).append(
            (datetime.fromisoformat(r["fetched_at"]), mid, r["sport"], r["venue"]))
    for key, pts in lseries.items():
        for (t0, m0, sport, venue), (t1, m1, *_ ) in zip(pts, pts[1:]):
            dt = (t1 - t0).total_seconds()
            if dt > 0:
                rates.setdefault((sport, venue), []).append(abs(m1 - m0) / Decimal(dt))
    for (sport, venue), vals in rates.items():
        velocities.setdefault(sport, {})[venue] = str(q6(statistics.median(vals)))

    out = {"pairs": results, "velocities": velocities,
           "excluded_no_bid_fraction": round(excluded, 4), "seed": seed}
    store.insert("leadlag_runs", {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "params_json": json.dumps({k: str(v) for k, v in ll.items()}),
        "results_json": json.dumps(out)})
    return out
