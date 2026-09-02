"""Where inside L13's [0.10,0.30) NO band are people actually wrong?

The band survived correction and a time split, but a pooled +0.0433 says
nothing about WHERE the mispricing lives. This conditions it on covariates we
can compute from the corpus we already hold.

Discipline, because this is exactly how a cell that was already exploratory
gets p-hacked into a result:
  * the factor list is PRE-DECLARED below and not edited after seeing output;
  * every cell tested counts toward one Bonferroni threshold, printed up front;
  * anything that survives is then split in TIME, and only a factor whose two
    arms BOTH exclude zero is reported as holding up.

The factor that decides whether any of this is tradeable is LIQUIDITY. If the
edge lives only where volume is zero, it is the taker lane's depth wall again
and not an opportunity.

Read-only. Usage: PYTHONPATH=. .venv/bin/python scripts/l13_factors.py
"""
from __future__ import annotations

import copy
import sqlite3
import statistics
import sys
from collections import defaultdict

from arb import bench_kalshi as bk
from arb.config import load

LO, HI = 0.10, 0.30
MIN_CELL = 60          # a cell smaller than this cannot inform anything
Z_UNCORRECTED = 1.96


def _at_cutoff(o, hours):
    """The last candle at or before the horizon, plus the one 2x back (drift)."""
    cs = o["candles"]
    close_ts = max(c.get("end_period_ts", 0) for c in cs)
    cutoff = close_ts - hours * 3600
    last = prev = None
    for c in cs:
        ts = c.get("end_period_ts", 0)
        if ts <= close_ts - 2 * hours * 3600:
            prev = c
        if ts > cutoff:
            break
        last = c
    return last, prev, close_ts


def _f(d, key):
    try:
        return float(d.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def covariates(obs, hours):
    """Attach the pre-declared factors to each observation."""
    per_event = defaultdict(int)
    for o in obs:
        per_event[o["event"]] += 1
    for o in obs:
        last, prev, close_ts = _at_cutoff(o, hours)
        if last is None:
            o["ok"] = False
            continue
        o["ok"] = True
        ask = _f(last.get("yes_ask") or {}, "close_dollars")
        bid = _f(last.get("yes_bid") or {}, "close_dollars")
        o["spread"] = ask - bid
        o["oi"] = _f(last, "open_interest_fp")
        o["vol"] = _f(last, "volume_fp")
        first_ts = min(c.get("end_period_ts", 0) for c in o["candles"])
        o["life_h"] = (close_ts - first_ts) / 3600.0
        prev_ask = _f(prev.get("yes_ask") or {}, "close_dollars") if prev else ask
        o["drift"] = ask - prev_ask          # + = favorite got MORE favored
        o["ladder"] = per_event[o["event"]]
    return [o for o in obs if o.get("ok")]


def score(cfg, rows):
    est, lo, hi, n, k, nc = bk._freq_minus_cost(
        cfg, [(o["cost"], o["won"], o["event"]) for o in rows])
    if not n:
        return None
    return dict(n=n, nc=nc, won=k / n, est=float(est), lo=float(lo), hi=float(hi))


def terciles(rows, key):
    vals = sorted(o[key] for o in rows)
    if len(vals) < 3:
        return []
    a, b = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    if a == b:                      # degenerate (e.g. volume mostly 0)
        return [(f"{key}==0", lambda o: o[key] <= a),
                (f"{key}>0", lambda o: o[key] > a)]
    return [(f"{key} low (<={a:.4g})", lambda o, a=a: o[key] <= a),
            (f"{key} mid", lambda o, a=a, b=b: a < o[key] <= b),
            (f"{key} high (>{b:.4g})", lambda o, b=b: o[key] > b)]


def main() -> int:
    cfg = load("config.yaml")
    hours = int(cfg["bench"]["horizon_hours"])
    hist = sqlite3.connect(f"file:{cfg['bench']['db']}?mode=ro", uri=True)
    hist.row_factory = sqlite3.Row
    close = {r["ticker"]: r["close_time"] for r in
             hist.execute("SELECT ticker, close_time FROM hist_markets")}

    obs = bk._horizon_observations(cfg, hist, {}, side="no")
    for o in obs:
        o["close"] = close.get(o["ticker"]) or ""
    band = [o for o in obs if LO <= float(o["cost"]) < HI and o["close"]]
    band = covariates(band, hours)
    band.sort(key=lambda o: o["close"])
    print(f"L13 NO band [{LO},{HI}) at T-{hours}h — n={len(band)}\n")

    # PRE-DECLARED factor list. Do not extend after reading the output.
    factors = [("liquidity: volume", terciles(band, "vol")),
               ("liquidity: open interest", terciles(band, "oi")),
               ("spread at horizon", terciles(band, "spread")),
               ("market lifetime", terciles(band, "life_h")),
               ("drift into horizon", terciles(band, "drift")),
               ("ladder size", [("ladder 1 (standalone)", lambda o: o["ladder"] == 1),
                                ("ladder 2-4", lambda o: 2 <= o["ladder"] <= 4),
                                ("ladder 5+", lambda o: o["ladder"] >= 5)])]
    cells = sum(len(b) for _, b in factors)
    zb = _bonf_z(cells)
    print(f"{cells} cells tested -> Bonferroni z threshold {zb:.2f} "
          f"(uncorrected {Z_UNCORRECTED})\n")

    survivors = []
    for name, bins in factors:
        print(f"=== {name} ===")
        print(f"{'cell':<30}{'n':>6}{'clus':>6}{'won':>8}{'edge':>9}"
              f"{'z':>7}   95% CI")
        print("-" * 84)
        for label, pred in bins:
            rows = [o for o in band if pred(o)]
            if len(rows) < MIN_CELL:
                continue
            s = score(cfg, rows)
            se = (s["hi"] - s["lo"]) / (2 * Z_UNCORRECTED)
            z = s["est"] / se if se else 0.0
            mark = ""
            if abs(z) > zb:
                mark = "  <- survives Bonferroni"
                survivors.append((f"{name}: {label}", pred))
            elif abs(z) > Z_UNCORRECTED:
                mark = "  <- uncorrected only"
            print(f"{label:<30}{s['n']:>6}{s['nc']:>6}{s['won']:>8.4f}"
                  f"{s['est']:>+9.4f}{z:>7.2f}   [{s['lo']:+.4f},{s['hi']:+.4f}]{mark}")
        print()

    print("=== temporal split of every Bonferroni survivor ===")
    if not survivors:
        print("none")
    for label, pred in survivors:
        rows = [o for o in band if pred(o)]
        mid = len(rows) // 2
        e, la = score(cfg, rows[:mid]), score(cfg, rows[mid:])
        if not e or not la:
            continue
        ok = e["lo"] > 0 and la["lo"] > 0 or (e["hi"] < 0 and la["hi"] < 0)
        print(f"\n{label}   (cut {rows[mid]['close'][:10]})")
        print(f"   EARLY n={e['n']:>5} {e['est']:+.4f} [{e['lo']:+.4f},{e['hi']:+.4f}]")
        print(f"   LATE  n={la['n']:>5} {la['est']:+.4f} [{la['lo']:+.4f},{la['hi']:+.4f}]")
        print(f"   -> {'HOLDS in both arms' if ok else 'FAILS — an arm includes zero'}")
    return 0


def _bonf_z(cells):
    # two-sided Bonferroni: alpha/2/cells via the normal quantile
    from statistics import NormalDist
    return NormalDist().inv_cdf(1 - 0.05 / (2 * max(cells, 1)))


if __name__ == "__main__":
    sys.exit(main())
