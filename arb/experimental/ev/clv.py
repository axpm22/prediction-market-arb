"""CLV grading — EV_SPEC §5.1, the prove-it-before-betting instrument.

For every recorded EV flag, ask one free question: did the venue's own price later drift
toward (past) your entry? clv_points = venue_close_mid - entry_ask. Positive across a book
of flags = the slow-gradient edge is real, demonstrated with ZERO settled games and zero
new fetching — this reads the EV flags and the quote archive only.

Decision rule (§5.1): if EV entries do not systematically beat the venue close (mean CLV
<= 0, CI excluding a meaningful positive), EV is dead. If they do, edge is shown before a
single bet. EV betting stays OFF; this is measurement.
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime
from decimal import Decimal
from statistics import mean

from arb.config import load_config
from arb.pricing import clv_points, q6
from arb.store import Store

# A flag's venue close is its outcome's last pregame quote on the SAME venue+book, mid of
# that book if available; here we use the recorded effective cost as the venue price proxy
# (same $1 unit as entry), subject to the depth floor that already gated the quote in.


def _ev_flags(store: Store) -> list[dict]:
    with store.connect() as con:
        rows = list(
            con.execute(
                "SELECT * FROM flat_bets WHERE kind = 'EV' AND status IN ('OPEN','REVIEW') ORDER BY id"
            )
        )
    flags = []
    for row in rows:
        legs = json.loads(row["legs_json"])
        if not legs:
            continue
        leg = legs[0]
        flags.append({
            "id": row["id"],
            "event_key": leg["event_key"],
            "venue": leg["venue"],
            "book": leg["book"],
            "outcome": leg["outcome_label"],
            "entry_ask": Decimal(str(leg["cost"])) + Decimal(str(leg["fee_cost"])),
            "placed_at": row["placed_at"],
        })
    return flags


def venue_close(store: Store, event_key: str, venue: str, book: str, outcome: str,
                min_depth: Decimal) -> Decimal | None:
    """Last pregame effective cost on the same venue book for this outcome (the close)."""
    with store.connect() as con:
        row = con.execute(
            """
            SELECT cost, fee_cost, depth_usd FROM quotes
             WHERE event_key = ? AND venue = ? AND book = ? AND outcome = ? AND live = 0
             ORDER BY fetched_at DESC LIMIT 1
            """,
            (event_key, venue, book, outcome),
        ).fetchone()
    if row is None:
        return None
    if row["depth_usd"] is not None and Decimal(str(row["depth_usd"])) < min_depth:
        return None  # §5.1 thin close fakes signal
    return Decimal(str(row["cost"])) + Decimal(str(row["fee_cost"]))


def grade_clv(store: Store, min_depth: Decimal) -> dict:
    flags = _ev_flags(store)
    graded: list[Decimal] = []
    for f in flags:
        close = venue_close(store, f["event_key"], f["venue"], f["book"], f["outcome"], min_depth)
        if close is None:
            continue
        graded.append(clv_points(close, f["entry_ask"]))
    if not graded:
        return {"n": 0, "flags": len(flags)}
    values = [float(g) for g in graded]
    return {
        "n": len(graded),
        "flags": len(flags),
        "mean_clv": round(mean(values), 5),
        "positive_rate": round(sum(1 for v in values if v > 0) / len(values), 3),
        "ci95": _bootstrap_ci(values),
    }


def _bootstrap_ci(values: list[float], resamples: int = 10000) -> tuple[float, float]:
    if len(values) < 2:
        return (round(values[0], 5), round(values[0], 5))
    rng = random.Random(12345)  # fixed seed → reproducible CI (pre-registration method)
    means = []
    n = len(values)
    for _ in range(resamples):
        means.append(mean(rng.choices(values, k=n)))
    means.sort()
    return (round(means[int(0.025 * resamples)], 5), round(means[int(0.975 * resamples)], 5))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arb.experimental.ev.clv")
    parser.add_argument("--min-depth", type=float, default=50.0)
    args = parser.parse_args(argv)
    store = Store(load_config().database_path)
    r = grade_clv(store, Decimal(str(args.min_depth)))
    if r["n"] == 0:
        print(f"No CLV-gradeable EV flags yet ({r.get('flags', 0)} flags, none with a depth-floored venue close).")
        return 0
    print(f"CLV over {r['n']} EV flags (of {r['flags']} recorded):")
    print(f"  mean CLV points : {r['mean_clv']:+.5f}")
    print(f"  positive rate   : {r['positive_rate']:.1%}")
    print(f"  95% bootstrap CI: [{r['ci95'][0]:+.5f}, {r['ci95'][1]:+.5f}]")
    verdict = "edge demonstrated" if r["ci95"][0] > 0 else "NOT proven (CI includes <= 0) — EV stays disabled"
    print(f"  verdict         : {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
