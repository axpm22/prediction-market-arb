"""Robustness of L13's positive band: category split, correction, time split.

A single band clearing an uncorrected CI is a hypothesis, not a result — L8's
own detail line says so. This applies the three checks that decide whether the
[0.10,0.30) NO band is worth believing:

  1. CATEGORY. The 2026 "domain-specific calibration" literature says these
     effects are not uniform across domains. Pooling can manufacture an edge
     out of one concentrated cell.
  2. CORRECTION. Six bands were searched, twelve across both lanes. Bonferroni
     thresholds are printed against the observed z.
  3. TIME. KNOWN_ISSUES #12 is the precedent: a +0.0429 pooled edge split into
     +0.0681 early / +0.0172 late and FAILED to confirm. The corpus spans
     2025-03 to 2026-07, so this split is free.

Read-only; the corpus is opened mode=ro. Usage:
    PYTHONPATH=. .venv/bin/python scripts/l13_robustness.py
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict

from arb import bench_kalshi as bk
from arb.config import load

LO, HI = 0.10, 0.30
MIN_CELL = 30


def _score(cfg, rows, label):
    est, lo, hi, n, k, nc = bk._freq_minus_cost(
        cfg, [(o["cost"], o["won"], o["event"]) for o in rows])
    if not n:
        print(f"{label:<34} (empty)")
        return None
    print(f"{label:<34}{n:>6}{nc:>6}{k / n:>8.4f}{float(est):>+9.4f}"
          f"   [{float(lo):+.4f},{float(hi):+.4f}]")
    return float(lo), float(est), float(hi)


def main() -> int:
    cfg = load("config.yaml")
    hist = sqlite3.connect(f"file:{cfg['bench']['db']}?mode=ro", uri=True)
    hist.row_factory = sqlite3.Row
    close = {r["ticker"]: r["close_time"] for r in
             hist.execute("SELECT ticker, close_time FROM hist_markets")}
    obs = bk._horizon_observations(cfg, hist, {}, side="no")
    for o in obs:
        o["close"] = close.get(o["ticker"]) or ""
    cell = [o for o in obs if LO <= float(o["cost"]) < HI and o["close"]]
    cell.sort(key=lambda o: o["close"])

    hdr = f"{'':<34}{'n':>6}{'clus':>6}{'won':>8}{'edge':>9}   95% CI"
    print(f"L13 NO band [{LO},{HI}) — n={len(cell)}\n")

    print("=== 1. by category ===")
    print(hdr)
    print("-" * 78)
    by = defaultdict(list)
    for o in cell:
        by[o["category"]].append(o)
    pos = tot = 0
    for cat, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if len(rows) < MIN_CELL:
            continue
        got = _score(cfg, rows, cat[:33])
        tot += 1
        pos += 1 if got and got[0] > 0 else 0
    print(f"\ncells n>={MIN_CELL}: {tot}; CI entirely above 0: {pos}")
    print("NOTE: clusters far below n means one event contributed many markets "
          "(strike ladders); the CI already shrinks to clusters, but a low "
          "cluster count still concentrates the estimate on few event-days.")

    print("\n=== 2. multiple comparisons ===")
    got = _score(cfg, cell, "pooled")
    lo, est, hi = got
    se = (hi - lo) / (2 * float(cfg["bench"]["ci_z"]))
    z = est / se if se else 0.0
    print(f"z={z:.2f}   uncorrected(1.96) {'PASS' if abs(z) > 1.96 else 'FAIL'}"
          f" | Bonferroni 6 bands(2.64) {'PASS' if abs(z) > 2.638 else 'FAIL'}"
          f" | 12 both lanes(2.87) {'PASS' if abs(z) > 2.87 else 'FAIL'}")

    print("\n=== 3. temporal split (rule 12 / KNOWN_ISSUES #12) ===")
    mid = len(cell) // 2
    cut = cell[mid]["close"][:10]
    print(hdr)
    print("-" * 78)
    e = _score(cfg, cell[:mid], f"EARLY  (..{cut})")
    la = _score(cfg, cell[mid:], f"LATE   ({cut}..)")
    ok = bool(e and la and e[0] > 0 and la[0] > 0)
    print(f"\nconfirms out-of-sample: "
          f"{'YES — both arms exclude zero' if ok else 'NO — an arm includes zero'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
