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
from arb.pricing import clv_points, devig_book_method, q6
from arb.store import Store

# A flag's venue close is its outcome's last pregame quote on the SAME venue+book, mid of
# that book if available; here we use the recorded effective cost as the venue price proxy
# (same $1 unit as entry), subject to the depth floor that already gated the quote in.


def _ev_flags(store: Store) -> list[dict]:
    with store.connect() as con:
        rows = list(
            con.execute(
                # N1: quarantined flags (quality_flag NOT NULL — uneconomic, below-min-profit,
                # low edge/SE, etc.) must NOT enter the edge-proof sample; they would bias the
                # gate that decides whether EV ever goes live. Only clean flags count.
                "SELECT * FROM flat_bets WHERE kind = 'EV' AND status IN ('OPEN','REVIEW') "
                "AND quality_flag IS NULL ORDER BY id"
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
    # Reuse ONE connection for all per-flag closes. The old code opened a fresh
    # connection (PRAGMA setup) per flag; the indexed LIMIT-1 lookup itself is sub-ms.
    graded: list[Decimal] = []
    seen: dict[tuple, Decimal | None] = {}
    with store.connect() as con:
        for f in flags:
            key = (f["event_key"], f["venue"], f["book"], f["outcome"])
            if key not in seen:
                row = con.execute(
                    """
                    SELECT cost, fee_cost, depth_usd FROM quotes
                     WHERE event_key=? AND venue=? AND book=? AND outcome=? AND live=0
                     ORDER BY fetched_at DESC LIMIT 1
                    """,
                    key,
                ).fetchone()
                if row is None or (row["depth_usd"] is not None and Decimal(str(row["depth_usd"])) < min_depth):
                    seen[key] = None
                else:
                    seen[key] = Decimal(str(row["cost"])) + Decimal(str(row["fee_cost"]))
            close = seen[key]
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


def _two_sided_devig_mid(con, event_key: str, venue: str, market_type, line, target: str,
                         at_or_before: str | None) -> Decimal | None:
    """EV#9: de-vig the two-sided book on this venue (each outcome's last raw price at/before
    `at_or_before`, or the close if None) and return the no-vig mid for `target`. Fees are
    NOT included — they belong in P&L, not the CLV signal."""
    line_clause = "line IS ?" if line in (None, "") else "line = ?"
    outcomes = [r[0] for r in con.execute(
        f"""SELECT DISTINCT outcome FROM quotes
             WHERE event_key=? AND venue=? AND market_type=? AND {line_clause} AND live=0""",
        (event_key, venue, market_type, line),
    )]
    if target not in outcomes or len(outcomes) < 2:
        return None
    raw: dict[str, Decimal] = {}
    for outcome in outcomes:
        params = [event_key, venue, market_type, line, outcome]
        time_clause = ""
        if at_or_before is not None:
            time_clause = "AND fetched_at <= ?"
            params.append(at_or_before)
        row = con.execute(
            f"""SELECT raw_price FROM quotes
                 WHERE event_key=? AND venue=? AND market_type=? AND {line_clause} AND outcome=?
                       AND live=0 {time_clause}
                 ORDER BY fetched_at DESC LIMIT 1""",
            params,
        ).fetchone()
        if row is None:
            return None
        price = Decimal(str(row["raw_price"]))
        if price <= 1:  # venue books store $1-unit cost as raw_price; need decimal odds to devig
            raw[outcome] = price
    if len(raw) == len(outcomes):
        # raw_price is already the $1-unit cost for venues; de-vig = renormalize the pair.
        total = sum(raw.values(), Decimal("0"))
        return q6(raw[target] / total) if total > 0 else None
    return None


def grade_clv_novig(store: Store) -> dict:
    """EV#9 amendment (H-EV1): CLV in NO-VIG MID space on both ends. closing_mid - entry_mid,
    each from the de-vigged two-sided venue book, fees excluded from the signal."""
    flags = _ev_flags(store)
    graded: list[float] = []
    with store.connect() as con:
        for f in flags:
            close_mid = _two_sided_devig_mid(con, f["event_key"], f["venue"], "WINNER", None, f["outcome"], None)
            entry_mid = _two_sided_devig_mid(con, f["event_key"], f["venue"], "WINNER", None, f["outcome"], f["placed_at"])
            if close_mid is None or entry_mid is None:
                continue
            graded.append(float(close_mid - entry_mid))  # >0 = venue fair value moved toward us
    if not graded:
        return {"n": 0, "flags": len(flags)}
    return {
        "n": len(graded), "flags": len(flags),
        "mean_clv": round(mean(graded), 5),
        "positive_rate": round(sum(1 for v in graded if v > 0) / len(graded), 3),
        "ci95": _bootstrap_ci(graded),
    }


def clv_result(store: Store, fee_loaded: bool, min_depth: Decimal) -> dict:
    """N1: the no-vig CLV is the DEFAULT verdict instrument. Loading fees on both ends
    (`grade_clv`) biases mean CLV toward/below zero, so the pre-registered gate
    `ci95_low > 0` produces a false negative on your own edge and keeps EV disabled. The
    fee-loaded metric stays available behind `--fee-loaded` for comparison only."""
    return grade_clv(store, min_depth) if fee_loaded else grade_clv_novig(store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arb.experimental.ev.clv")
    parser.add_argument("--min-depth", type=float, default=50.0,
                        help="depth floor for the fee-loaded venue close (only used with --fee-loaded)")
    parser.add_argument("--fee-loaded", action="store_true",
                        help="N1: use the BIASED fee-loaded close-minus-entry CLV (comparison only; not the gate)")
    # Back-compat: --novig was the opt-in for the no-vig metric; it is now the default, so the
    # flag is accepted but a no-op (the prompt's `clv --novig` still works).
    parser.add_argument("--novig", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    store = Store(load_config().database_path)
    r = clv_result(store, fee_loaded=args.fee_loaded, min_depth=Decimal(str(args.min_depth)))
    metric = "fee-loaded (BIASED — comparison only)" if args.fee_loaded else "no-vig mid (default gate)"
    if r["n"] == 0:
        print(f"No CLV-gradeable EV flags yet ({r.get('flags', 0)} flags, none with a depth-floored venue close).")
        return 0
    print(f"CLV [{metric}] over {r['n']} EV flags (of {r['flags']} recorded):")
    print(f"  mean CLV points : {r['mean_clv']:+.5f}")
    print(f"  positive rate   : {r['positive_rate']:.1%}")
    print(f"  95% bootstrap CI: [{r['ci95'][0]:+.5f}, {r['ci95'][1]:+.5f}]")
    verdict = "edge demonstrated" if r["ci95"][0] > 0 else "NOT proven (CI includes <= 0) — EV stays disabled"
    print(f"  verdict         : {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
