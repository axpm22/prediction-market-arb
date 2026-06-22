"""Winner/loser diagnostic for settled paper bets.

Mines the append-only `flat_bets` ledger to answer: *where does the edge (if any)
actually live, and where is it bleeding?* It slices settled bets by feature
(market type, price band, flagged-edge band, venue, fill-confirmability, day),
reports win rate + paper P&L + a bootstrap 95% CI per slice, and labels each slice
SIGNAL / NOISE / INSUFFICIENT.

IMPORTANT — this is *hypothesis generation*, not a strategy. Slicing one settled
sample and keeping the positive slices is how you overfit noise. Any rule this
surfaces must be (a) pre-registered and (b) re-tested on NEW data before it is
trusted. The built-in chronological train/test split (see `oos_check`) is the
minimum honesty bar: derive the rule on the first part of history, apply it to the
held-out part, and see whether the edge survives.

Stats here are display/analysis only (float is fine); money math in the ledger
stays Decimal per the project rules.

Run:  python -m arb.analysis.diagnose [db_path] [--kind EV]
"""
from __future__ import annotations
import argparse
import json
import random
import sqlite3
import statistics as st
from typing import Callable

random.seed(0)  # reproducible bootstrap


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(db_path: str, kind: str) -> list[dict]:
    """Pull settled bets of one kind, flattening the first leg's features."""
    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(flat_bets)").fetchall()}
    validation_rule_expr = "validation_rule" if "validation_rule" in columns else "NULL AS validation_rule"
    validation_tag_expr = "validation_tag" if "validation_tag" in columns else "NULL AS validation_tag"
    pnl_confirmed_expr = "pnl_confirmed" if "pnl_confirmed" in columns else "NULL AS pnl_confirmed"
    rows = con.execute(
        f"""select edge, optimistic_cost, confirmed_cost, pnl_optimistic,
                  {pnl_confirmed_expr}, result, sport, category, trusted, legs_json, placed_at,
                  {validation_rule_expr}, {validation_tag_expr}
             from flat_bets where kind=? and status='SETTLED'""",
        (kind,),
    ).fetchall()
    con.close()
    recs = []
    for edge, oc, cc, po, pc, res, sport, cat, trusted, legs, placed, validation_rule, validation_tag in rows:
        try:
            leg = json.loads(legs)[0]
        except Exception:
            leg = {}
        cost = _f(oc) if _f(oc) is not None else _f(leg.get("cost"))
        recs.append(
            dict(
                edge=_f(edge),
                cost=cost,
                pnl=_f(pc) if _f(pc) is not None else (_f(po) or 0.0),
                pnl_optimistic=_f(po) or 0.0,
                pnl_confirmed=_f(pc),
                won=(res == "WON"),
                sport=sport or leg.get("sport"),
                venue=leg.get("venue"),
                mtype=leg.get("market_type"),
                trusted=bool(trusted),
                confirmed=(cc is not None),
                day=(placed or "")[:10],
                validation_rule=validation_rule,
                validation_tag=validation_tag,
            )
        )
    return recs


def bootstrap_ci(vals: list[float], n_boot: int = 2000, alpha: float = 0.05):
    """Bootstrap CI for the mean of per-bet P&L."""
    if len(vals) < 2:
        return (None, None)
    means = []
    n = len(vals)
    for _ in range(n_boot):
        means.append(sum(random.choices(vals, k=n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[int((1 - alpha / 2) * n_boot)]
    return (lo, hi)


def verdict(n: int, lo, hi, min_n: int = 30) -> str:
    if n < min_n:
        return "INSUFFICIENT"
    if lo is None:
        return "INSUFFICIENT"
    if lo > 0:
        return "SIGNAL+"      # positive, CI clears zero
    if hi < 0:
        return "SIGNAL-"      # reliably losing -> filter OUT
    return "noise"             # CI straddles zero


def summarize(recs: list[dict]) -> dict:
    pnls = [r["pnl"] for r in recs]
    n = len(recs)
    wins = sum(1 for r in recs if r["won"])
    total = sum(pnls)
    lo, hi = bootstrap_ci(pnls)
    return dict(
        n=n,
        wr=(wins / n if n else 0.0),
        total=total,
        per_bet=(total / n if n else 0.0),
        ci_lo=lo,
        ci_hi=hi,
        verdict=verdict(n, lo, hi),
    )


def breakdown(recs: list[dict], key: Callable[[dict], str], label: str):
    print(f"\n=== by {label} ===")
    print(f"  {'slice':22s} {'n':>4s} {'wr':>5s} {'$total':>8s} "
          f"{'$/bet':>7s} {'95% CI /bet':>20s}  verdict")
    groups: dict[str, list[dict]] = {}
    for r in recs:
        groups.setdefault(str(key(r)), []).append(r)
    for k in sorted(groups):
        s = summarize(groups[k])
        ci = (f"[{s['ci_lo']:+.3f},{s['ci_hi']:+.3f}]"
              if s["ci_lo"] is not None else "  n/a")
        print(f"  {k:22s} {s['n']:4d} {s['wr']:5.2f} {s['total']:+8.2f} "
              f"{s['per_bet']:+7.3f} {ci:>20s}  {s['verdict']}")


# ---- feature bucketers ---------------------------------------------------
def cost_band(r):
    c = r["cost"]
    if c is None:
        return "cost?"
    for hi, name in [(0.2, "0.0-0.2"), (0.4, "0.2-0.4"), (0.6, "0.4-0.6"),
                     (0.8, "0.6-0.8"), (1.01, "0.8-1.0")]:
        if c < hi:
            return name
    return "0.8-1.0"


def edge_band(r):
    e = r["edge"] or 0.0
    for hi, name in [(0.01, "edge<0.01"), (0.02, "edge0.01-0.02"),
                     (0.05, "edge0.02-0.05"), (0.25, "edge0.05-0.25")]:
        if e < hi:
            return name
    return "edge>0.25"


def evaluate_rule(recs: list[dict], pred: Callable[[dict], bool], name: str):
    keep = [r for r in recs if pred(r)]
    drop = [r for r in recs if not pred(r)]
    print(f"\n=== candidate attack rule: {name} ===")
    for tag, sub in [("KEEP (rule passes)", keep), ("DROP (rule rejects)", drop)]:
        s = summarize(sub)
        ci = (f"[{s['ci_lo']:+.3f},{s['ci_hi']:+.3f}]"
              if s["ci_lo"] is not None else "n/a")
        print(f"  {tag:22s} n={s['n']:4d} wr={s['wr']:.2f} "
              f"${s['total']:+7.2f} ${s['per_bet']:+.3f}/bet CI={ci} {s['verdict']}")


def oos_check(recs: list[dict], pred: Callable[[dict], bool],
              name: str, frac: float = 0.6):
    """Chronological train/test: the rule is fixed; we just check it on the
    held-out later bets it never 'saw'. This is the anti-overfitting gate."""
    rs = sorted(recs, key=lambda r: r["day"])
    split = int(len(rs) * frac)
    train, test = rs[:split], rs[split:]
    print(f"\n=== out-of-sample stability for: {name} "
          f"(train {len(train)} / test {len(test)} by date) ===")
    for tag, sub in [("in-sample (train)", train), ("OUT-OF-SAMPLE (test)", test)]:
        kept = [r for r in sub if pred(r)]
        s = summarize(kept)
        print(f"  {tag:22s} kept n={s['n']:3d} wr={s['wr']:.2f} "
              f"${s['total']:+6.2f} ${s['per_bet']:+.3f}/bet  {s['verdict']}")
    print("  -> trust the rule only if OUT-OF-SAMPLE stays positive.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/arb.sqlite")
    ap.add_argument("--kind", default="EV")
    ap.add_argument("--since", default=None,
                    help="only score bets placed on/after this YYYY-MM-DD "
                         "(use the pre-registration date for an honest forward test)")
    ap.add_argument("--validation-tag", default=None,
                    help="only score rows carrying this validation_tag (for example KEEP)")
    ap.add_argument("--validation-rule", default=None,
                    help="only score rows carrying this validation_rule (for example H-EVFILTER)")
    args = ap.parse_args()

    recs = load(args.db, args.kind)
    if args.since:
        recs = [r for r in recs if r["day"] >= args.since]
        print(f"# forward-test mode: only bets placed >= {args.since}")
    if args.validation_rule:
        recs = [r for r in recs if r["validation_rule"] == args.validation_rule]
        print(f"# validation-rule mode: {args.validation_rule}")
    if args.validation_tag:
        recs = [r for r in recs if r["validation_tag"] == args.validation_tag]
        print(f"# validation-tag mode: {args.validation_tag}")
    overall = summarize(recs)
    overall_ci = (
        f"[{overall['ci_lo']:+.3f},{overall['ci_hi']:+.3f}]"
        if overall["ci_lo"] is not None else "n/a"
    )
    print(f"# Diagnostic for kind={args.kind}: {overall['n']} settled bets, "
          f"wr={overall['wr']:.3f}, total=${overall['total']:+.2f}, "
          f"per-bet=${overall['per_bet']:+.3f}, "
          f"CI={overall_ci} "
          f"({overall['verdict']})")

    breakdown(recs, lambda r: r["mtype"], "market_type")
    breakdown(recs, cost_band, "price/cost band")
    breakdown(recs, edge_band, "flagged-edge band")
    breakdown(recs, lambda r: r["venue"], "venue")
    breakdown(recs, lambda r: "confirmed" if r["confirmed"] else "never-confirmed",
              "fill confirmability")
    breakdown(recs, lambda r: r["validation_tag"] or "untagged", "validation tag")

    # Attack rule derived from the breakdowns: keep mid-priced, low-flagged-edge,
    # confirmable bets; reject cheap longshots and high-flagged-edge (contaminated).
    rule = (lambda r: r["cost"] is not None and 0.40 <= r["cost"] <= 0.75
            and (r["edge"] or 0) < 0.02 and r["confirmed"])
    name = "cost in [0.40,0.75] AND flagged_edge<0.02 AND confirmed-fillable"
    evaluate_rule(recs, rule, name)
    oos_check(recs, rule, name)

    print("\n# REMINDER: in-sample slicing generates hypotheses, not strategies.")
    print("# Pre-register any surviving rule, then prove it on fresh settled bets.")


if __name__ == "__main__":
    main()
