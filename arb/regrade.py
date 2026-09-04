"""`arb regrade` (§18.3 Part B): read-only paired-Brier re-scoring of consensus
(soft-book median vs Pinnacle anchor) against venue mids, over settled MLB
WINNER events. Two comparison bases — as-stored and closing (the staleness-
artifact control, trap 13) — plus the EV-subset win-rate re-open condition.
Cluster-bootstrapped by event_key (never by row). Reads ONLY the local DB and
the keyless MLB Stats API; never the live Odds API (rule 2), never writes any
table (rule 9 ethos) — its only outputs are the caller's stdout and
reports/regrade-<date>.md."""
from __future__ import annotations

import random
import statistics
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from arb.logutil import get_logger
from arb.matching import load_aliases, normalize_team

log = get_logger("arb.regrade", "logs/regrade.log")


def _method_bucket(method: str) -> str:
    return "anchor" if method.startswith("anchor:") else "median"


def resolve_finals(cfg, store) -> dict[str, tuple[str, datetime]]:
    """event_key -> (winner_canonical, start_utc) for every resolvable MLB WINNER
    event referenced by stored consensus rows. An event with no final, or an
    ambiguous same-day final with no :dhN to disambiguate, is simply absent —
    the caller counts the gap, never guesses (rule 3 ethos)."""
    from arb.venues.mlbstats import fetch_schedule, is_final
    aliases = load_aliases()
    event_keys = {r["event_key"] for r in store.query(
        "SELECT DISTINCT event_key FROM consensus WHERE market_type='WINNER'")
        if r["event_key"].startswith("baseball_mlb:")}
    dates = sorted({ek.split(":")[1] for ek in event_keys if len(ek.split(":")) >= 4})
    games_by_date: dict[str, list[dict]] = {}
    for d in dates:
        try:
            games_by_date[d] = [g for g in fetch_schedule(cfg, d) if is_final(g)
                                and g["home_score"] is not None
                                and g["away_score"] is not None]
        except Exception as e:  # noqa: BLE001 — one bad date must not kill the run
            log.warning("regrade: mlb schedule fetch failed for %s: %s", d, e)
            games_by_date[d] = []

    out: dict[str, tuple[str, datetime]] = {}
    for ek in event_keys:
        parts = ek.split(":")
        if len(parts) < 4:
            continue
        date, names = parts[1], parts[2:4]
        dh = int(parts[4][2:]) if len(parts) > 4 and parts[4].startswith("dh") else None
        pair = set(names)
        finals = [g for g in games_by_date.get(date, [])
                  if {normalize_team("baseball_mlb", g["home"], aliases),
                      normalize_team("baseball_mlb", g["away"], aliases)} == pair]
        if not finals:
            continue
        if dh is None:
            if len(finals) > 1:
                continue  # ambiguous doubleheader final with no :dhN — refuse
            g = finals[0]
        else:
            ordered = sorted(finals, key=lambda g: g.get("gameNumber") or 1)
            if dh > len(ordered):
                continue
            g = ordered[dh - 1]
        winner = normalize_team("baseball_mlb", g["home"], aliases) \
            if g["home_score"] > g["away_score"] \
            else normalize_team("baseball_mlb", g["away"], aliases)
        out[ek] = (winner, g["start_utc"])
    return out


def _quote_series(store) -> dict[tuple, list[tuple[datetime, Decimal]]]:
    """(event_key, outcome) -> [(fetched_at, mid)] ascending, YES-side WINNER
    quotes with an observed bid (mid = (cost+bid)/2, the project's standard)."""
    rows = store.query(
        """SELECT event_key, outcome, fetched_at, cost, bid FROM quotes
           WHERE sport = 'baseball_mlb' AND market_type = 'WINNER' AND side = 'YES'
             AND bid IS NOT NULL""")
    series: dict[tuple, list] = {}
    for r in rows:
        mid = (Decimal(r["cost"]) + Decimal(r["bid"])) / 2
        series.setdefault((r["event_key"], r["outcome"]), []).append(
            (datetime.fromisoformat(r["fetched_at"]), mid))
    for pts in series.values():
        pts.sort(key=lambda p: p[0])
    return series


def _nearest(points: list, target: datetime, tolerance_secs: int) -> Decimal | None:
    best = None
    for t, mid in points:
        dt = abs((t - target).total_seconds())
        if dt <= tolerance_secs and (best is None or dt < best[0]):
            best = (dt, mid)
    return best[1] if best else None


def _last_before(points: list, start: datetime, window_secs: int) -> Decimal | None:
    candidates = [(t, mid) for t, mid in points if t < start]
    if not candidates:
        return None
    t, mid = candidates[-1]
    return mid if (start - t).total_seconds() <= window_secs else None


def _consensus_rows(store, bucket: str) -> list[dict]:
    return [dict(r) for r in store.query("SELECT * FROM consensus WHERE market_type='WINNER'")
            if _method_bucket(r["method"]) == bucket]


def _score_bucket(cfg, rows: list[dict], quote_series: dict, finals: dict, aliases: dict):
    """-> (as_stored, closing, ev_wins, exclusions) — the first three keyed
    event_key -> [values]; ev_wins values are 0/1 wins, the others Brier diffs."""
    tol = int(cfg["regrade"]["match_tolerance_secs"])
    window = int(cfg["ev"]["clv_window_mins"]) * 60
    e_min = float(cfg["ev"]["e_min"])
    by_event: dict[str, list[dict]] = {}
    for r in rows:
        if r["event_key"].startswith("baseball_mlb:"):
            by_event.setdefault(r["event_key"], []).append(r)

    as_stored: dict[str, list[float]] = {}
    closing: dict[str, list[float]] = {}
    ev_wins: dict[str, list[int]] = {}
    excl = {"no_final": 0, "out_of_tolerance": 0}

    for ek, ek_rows in by_event.items():
        if ek not in finals:
            excl["no_final"] += len(ek_rows)
            continue
        winner, start = finals[ek]

        # as-stored: EVERY consensus row vs the nearest venue mid in tolerance
        latest_per_outcome: dict[str, tuple[datetime, dict]] = {}
        for r in ek_rows:
            outcome = normalize_team("baseball_mlb", r["outcome"], aliases) or r["outcome"]
            y = 1.0 if outcome == winner else 0.0
            p = float(r["p_hat"])
            computed_at = datetime.fromisoformat(r["computed_at"])
            mid = _nearest(quote_series.get((ek, outcome), []), computed_at, tol)
            if mid is None:
                excl["out_of_tolerance"] += 1
            else:
                mid = float(mid)
                as_stored.setdefault(ek, []).append((p - y) ** 2 - (mid - y) ** 2)
                if abs(p - mid) >= e_min:
                    win = 1 if (p > mid) == (y == 1.0) else 0
                    ev_wins.setdefault(ek, []).append(win)
            cur = latest_per_outcome.get(outcome)
            if cur is None or computed_at > cur[0]:
                latest_per_outcome[outcome] = (computed_at, r)

        # closing: LAST consensus row per outcome + LAST venue mid strictly
        # before start, BOTH within clv_window_mins of start (trap 13)
        for outcome, (computed_at, r) in latest_per_outcome.items():
            if computed_at >= start or (start - computed_at).total_seconds() > window:
                continue
            mid = _last_before(quote_series.get((ek, outcome), []), start, window)
            if mid is None:
                continue
            y = 1.0 if outcome == winner else 0.0
            p = float(r["p_hat"])
            closing.setdefault(ek, []).append((p - y) ** 2 - (float(mid) - y) ** 2)

    return as_stored, closing, ev_wins, excl


def _bootstrap_mean_ci(by_event: dict, seed: int, n: int,
                       percentiles: tuple[float, float] = (0.025, 0.975)
                       ) -> tuple[float, float, float]:
    """Cluster bootstrap by event_key -> (mean, ci_low, ci_high). Resample event
    keys with replacement; a drawn event contributes ALL its values (rows travel
    together) — this doubles as the win-rate bootstrap (mean of 0/1 = win rate).

    `percentiles` is the pair to read off the resampled means. The default is
    the two-sided 95% every regrade caller has always used; L14 passes the
    one-sided pairs its pre-registration names (design §12: "upper 90% bound"
    is the 90th percentile, "lower 80% bound" the 20th)."""
    all_vals = [v for vs in by_event.values() for v in vs]
    mean = statistics.mean(all_vals) if all_vals else 0.0
    events = list(by_event)
    if not events:
        return mean, mean, mean
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        sample = []
        for ek in (rng.choice(events) for _ in events):
            sample.extend(by_event[ek])
        if sample:
            means.append(statistics.mean(sample))
    means.sort()
    lo = means[int(percentiles[0] * len(means))]
    hi = means[int(percentiles[1] * len(means))]
    return mean, lo, hi


def _cell(by_event: dict, seed: int, n_boot: int, min_events: int) -> dict:
    n_events, n_pairs = len(by_event), sum(len(v) for v in by_event.values())
    if n_events < min_events:
        return {"insufficient": True, "n_events": n_events, "n_pairs": n_pairs}
    mean, lo, hi = _bootstrap_mean_ci(by_event, seed, n_boot)
    return {"insufficient": False, "n_events": n_events, "n_pairs": n_pairs,
            "mean": round(mean, 6), "ci_low": round(lo, 6), "ci_high": round(hi, 6)}


def run(cfg, store) -> dict:
    rc = cfg["regrade"]
    seed, n_boot, min_events = int(rc["bootstrap_seed"]), int(rc["bootstrap_n"]), int(rc["min_events"])
    aliases = load_aliases()
    finals = resolve_finals(cfg, store)
    quote_series = _quote_series(store)
    result = {"buckets": {}, "exclusions": {}}
    for bucket in ("median", "anchor"):
        rows = _consensus_rows(store, bucket)
        as_stored, closing, ev_wins, excl = _score_bucket(cfg, rows, quote_series, finals, aliases)
        result["buckets"][bucket] = {
            "as_stored": _cell(as_stored, seed, n_boot, min_events),
            "closing": _cell(closing, seed, n_boot, min_events),
            "ev_subset": _cell(ev_wins, seed, n_boot, min_events),
        }
        result["exclusions"][bucket] = excl
    return result


_BASES = (("as_stored", "mean_diff (lower=consensus better)"),
          ("closing", "mean_diff (lower=consensus better)"),
          ("ev_subset", "win_rate (>0.5=consensus better)"))


def write_report(result: dict, today: str, outdir: str | Path = "reports") -> Path:
    md = [f"# arb regrade {today}", "",
          "Read-only paired-Brier re-scoring vs venue mids (§18.3, PREREGISTRATION.md "
          "\"EV lane\" re-open trigger). `p_consensus` vs `p_venue_mid`, paired per "
          "event-outcome, cluster-bootstrapped 95% CI by event_key.", ""]
    for bucket in ("median", "anchor"):
        md.append(f"## {bucket}")
        md.append("| basis | metric | n_events | n_pairs | value | 95% CI |")
        md.append("|---|---|---|---|---|---|")
        for basis, label in _BASES:
            c = result["buckets"][bucket][basis]
            if c["insufficient"]:
                md.append(f"| {basis} | {label} | {c['n_events']} | {c['n_pairs']} | "
                          f"INSUFFICIENT n={c['n_events']} | — |")
            else:
                md.append(f"| {basis} | {label} | {c['n_events']} | {c['n_pairs']} | "
                          f"{c['mean']} | [{c['ci_low']}, {c['ci_high']}] |")
        excl = result["exclusions"][bucket]
        md.append(f"\n- excluded: no-final={excl['no_final']}, "
                  f"out-of-tolerance={excl['out_of_tolerance']}\n")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"regrade-{today}.md"
    path.write_text("\n".join(md) + "\n")
    return path
