"""Does L13's finding replicate on POLYMARKET?

L13 found that Kalshi favorites priced 0.70-0.90 win only ~70% of the time, so
buying the other side at 0.10-0.30 paid +0.0433 after fees. This asks the same
question of Polymarket, using the archive's own quotes for the price and
gamma for the outcome.

One structural difference matters. Kalshi candles quote only `yes_ask`/`yes_bid`,
so L13 had to DERIVE the no-ask as 1 - yes_bid. Polymarket quotes every outcome
token separately and the archive stores each as its own row, so the 0.10-0.30
band here is a DIRECTLY QUOTED ask on the underdog token — no inversion, and no
mirror identity. That makes this a genuinely independent test rather than the
arithmetic reflection L13 is.

Price: the last executable ask at or before T-24h, matching L8/L13's horizon.
Outcome: arb.reconcile._polymarket, the repo's existing resolver (keyless
gamma, `closed=true` load-bearing per KNOWN_ISSUES #13).

Read-only on the archive; writes nothing. Public unauthenticated GETs only
(rule 1), zero Odds API credits (rule 2).

Usage: PYTHONPATH=. .venv/bin/python scripts/pm_calibration.py [BAND_LO] [BAND_HI]
"""
from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal

from arb import bench_kalshi as bk
from arb import reconcile
from arb.config import load
from arb.models import q6
from arb.venues.fees import leg_fee_cost

SQL = """
WITH cand AS (
  SELECT venue_market_id, event_key, category, sport, outcome, side, cost,
         start_utc, fetched_at,
         ROW_NUMBER() OVER (PARTITION BY venue_market_id
                            ORDER BY fetched_at DESC) rn
  FROM quotes
  WHERE venue='polymarket' AND start_utc IS NOT NULL
    AND start_utc < '2026-08-25'
    AND fetched_at <= datetime(replace(substr(start_utc,1,19),'T',' '), '-24 hours')
)
SELECT * FROM cand
WHERE rn=1 AND CAST(cost AS REAL) >= ? AND CAST(cost AS REAL) < ?
"""


def main() -> int:
    lo_b = float(sys.argv[1]) if len(sys.argv) > 1 else 0.10
    hi_b = float(sys.argv[2]) if len(sys.argv) > 2 else 0.30
    cfg = load("config.yaml")
    con = sqlite3.connect("file:data/arb.sqlite?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(SQL, (lo_b, hi_b)).fetchall()
    print(f"PM band [{lo_b},{hi_b}) at T-24h: {len(rows)} markets to resolve\n")

    obs, won_n, unresolved, partial = [], 0, 0, 0
    for i, r in enumerate(rows, 1):
        verdict = reconcile._polymarket(cfg, {"venue_market_id": r["venue_market_id"]})
        if verdict is None:
            unresolved += 1
        elif isinstance(verdict, tuple):
            partial += 1                      # UMA fractional — excluded, not coerced
        else:
            ask = Decimal(r["cost"])
            cat = r["category"] or "sports"
            allin = q6(ask + leg_fee_cost(cfg, "polymarket", cat, ask,
                                          Decimal(1), "paper", "taker"))
            w = 1 if verdict == "WON" else 0
            won_n += w
            obs.append((allin, w, r["event_key"]))
        if i % 50 == 0:
            print(f"  ...{i}/{len(rows)} resolved={len(obs)} "
                  f"unresolved={unresolved}", flush=True)

    print(f"\nresolved {len(obs)} | unresolved {unresolved} | "
          f"UMA-partial {partial} (excluded)")
    if not obs:
        print("nothing resolved — cannot score")
        return 0
    est, lo, hi, n, k, nc = bk._freq_minus_cost(cfg, obs)
    mean_cost = sum(float(c) for c, _, _ in obs) / n
    print(f"\n{'':<12}{'n':>6}{'clus':>6}{'won':>8}{'cost':>8}{'edge':>9}   95% CI")
    print("-" * 70)
    print(f"{'PM band':<12}{n:>6}{nc:>6}{k / n:>8.4f}{mean_cost:>8.4f}"
          f"{float(est):>+9.4f}   [{float(lo):+.4f},{float(hi):+.4f}]")
    se = (float(hi) - float(lo)) / (2 * float(cfg["bench"]["ci_z"]))
    print(f"z = {float(est) / se:.2f}" if se else "")
    print("\nNOTE: this is a DIRECTLY QUOTED ask on the underdog token, not the "
          "1-yes_bid mirror L13 uses, so it is an independent test — but the "
          "archive spans only 2026-07-08..08-09, so n is small and the sample "
          "is one month of mostly-sports markets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
