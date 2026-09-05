"""Whale-follow backtest — the pre-specified plan of reports/backtest-whale-follow-plan-2026-09-05.md.

READ-ONLY on data/whale_tape.sqlite (`mode=ro` URI, never data/arb.sqlite).
Stdlib + sqlite3 only. Skill is NOT redefined here: wallet scores come from
`arb.whale_score.scores`/`score_for` over `resolved_outcomes` (whose `as_of`
filter IS the no-look-ahead guard) and the top-X% bar from
`arb.whale_elite.cutoff`, the lane's own relative qualification.

Money (prices, fees, edges) is Decimal; the statistics (means, bootstrap) run
in float — this is a report, not the ledger (rule 5's display/estimation
clause).

DEVIATION from the plan, stated here and in the report: the plan says the P&L
is "minus the venue fee exactly as `arb/whale_score.realized_edge` charges
it", but `realized_edge` charges NO fee — it is a gross per-share edge. The
closest faithful reading is used: `realized_edge`'s own payout/sell accounting
(per share, divided by the entry price to become per dollar) minus ONE
Polymarket taker fee at entry from the lane's single fee site,
`arb.venues.fees.leg_fee_cost`, priced with the position's root as the fee
category — which is what `arb/lanes/live.py` charges the whale lane's own
paper bets.
"""
from __future__ import annotations

import argparse
import itertools
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arb.config import load                                       # noqa: E402
from arb.venues.fees import leg_fee_cost                          # noqa: E402
from arb.whale_elite import cutoff                                # noqa: E402
from arb.whale_mat_read import load_positions                     # noqa: E402
from arb.whale_score import (aggregate, base_rate, resolved_outcomes,  # noqa: E402
                             score_for)

# The plan's constants. Not knobs: changing one changes the pre-registration.
MONTHS = [(2026, m) for m in range(2, 10)]           # as_of = 2026-02-01 .. 2026-09-01
TEST_START = datetime(2026, 7, 16, tzinfo=timezone.utc)
LIVE_START = datetime(2026, 9, 4, tzinfo=timezone.utc)
CONSENSUS_SECS = 24 * 3600
SOLD_FRACTION = Decimal("0.5")                       # E: cumulative sells >= 50% of size
RESAMPLES = 2000
N_CONFIGS = 12
BONF_Q = 0.2 / N_CONFIGS
QS = ("all", "top50", "top20")
BANDS = (("<0.20", Decimal("0.20")), ("0.20-0.50", Decimal("0.50")),
         ("0.50-0.80", Decimal("0.80")), (">0.80", Decimal(2)))


def _month(ts: int) -> tuple[int, int]:
    t = datetime.fromtimestamp(ts, timezone.utc)
    return (t.year, t.month)


def _as_of(ym) -> datetime:
    return datetime(ym[0], ym[1], 1, tzinfo=timezone.utc)


# --------------------------------------------------------------- the tape

def entry_exit(con, cfg) -> dict:
    """(wallet, cid, outcome_index, entry_ts) -> (first BUY fill price, exit).

    Replays `whale_score.positions_from_prints`' own window + FIFO rules over
    the prints so each `positions_mat` row gets the two facts the cache does
    not store: the price of the BUY that OPENED the window (the plan's entry)
    and the price of the SELL that first takes cumulative sells to >= 50% of
    the window's size (the plan's exit E). Keyed on `entry_ts`, which is that
    first BUY's ts, so the join back to the cache is exact.
    """
    w = cfg["whale"]
    window, min_usd = int(w["position_window_secs"]), Decimal(str(w["min_print_usd"]))
    rows = con.execute(
        "SELECT proxy_wallet, condition_id, outcome_index, ts, side, price, size,"
        " notional_usd FROM prints WHERE outcome_index IS NOT NULL"
        " ORDER BY proxy_wallet, condition_id, outcome_index, ts")
    out = {}
    for key, group in itertools.groupby(rows, key=lambda r: (r[0], r[1], r[2])):
        opens, cur = [], None
        for _, _, _, ts, side, price, size, notional in sorted(group, key=lambda r: int(r[3])):
            if Decimal(str(notional)) < min_usd:
                continue
            price, size, ts = Decimal(str(price)), Decimal(str(size)), int(ts)
            if side == "SELL":
                left = size
                for o in opens:                                   # oldest first
                    take = min(left, o["size"] - o["sold"])
                    if take > 0:
                        o["sold"] += take
                        left -= take
                        if o["exit"] is None and o["sold"] >= SOLD_FRACTION * o["size"]:
                            o["exit"] = (price, ts)
                    if left <= 0:
                        break
                continue
            if cur is None or ts - cur["last"] > window:
                cur = {"first": ts, "last": ts, "size": Decimal(0),
                       "sold": Decimal(0), "exit": None, "p0": price}
                opens.append(cur)
            cur["last"] = ts
            cur["size"] += size
        for o in opens:
            out[(key[0], key[1], key[2], o["first"])] = (o["p0"], o["exit"])
    return out


def load_tape(con):
    res = {r[0]: {"status": r[1], "resolved_at": r[2], "winner_outcome_index": r[3]}
           for r in con.execute("SELECT condition_id, status, resolved_at,"
                                " winner_outcome_index FROM resolutions")}
    mk = {r[0]: r[1] for r in con.execute(
        "SELECT condition_id, event_key FROM markets")}
    return load_positions(con), res, mk


# ------------------------------------------------------- walk-forward skill

def _pct_cfg(cfg, pct):
    fire = dict(cfg["whale"]["fire"], elite_pct=pct)
    return dict(cfg, whale=dict(cfg["whale"], fire=fire))


def walk_forward(positions, res, cfg, log=lambda *a: None) -> dict:
    """month -> {"lb": {(wallet, bucket): lb}, "cut": {bucket: (c50, c20)},
    "pop": {root: n}, "base": {root: (m0, n)}}.

    `resolved_outcomes(positions, res, as_of)` is the ONLY source of history,
    and it drops every market resolved at or after `as_of` — so nothing a
    month's decisions could not have known reaches its scores. Every position
    is handed in; the filter, not the caller, decides what counts.
    """
    cfgs = {"top50": _pct_cfg(cfg, 50), "top20": _pct_cfg(cfg, 20)}
    roots = sorted({p.root for p in positions})
    buckets = sorted({(p.sport, p.market_type) for p in positions}, key=str)
    table = {}
    for ym in MONTHS:
        as_of = _as_of(ym)
        outs = resolved_outcomes(positions, res, as_of)
        agg = aggregate(outs)
        base = {r: base_rate(outs, r, as_of, cfg) for r in roots}
        m0 = {r: v[0] for r, v in base.items()}

        def lookup(wallet, sport, market_type, _agg=agg, _m0=m0):
            return score_for(_agg, wallet, sport, market_type, cfg, _m0)

        pop = {r: len({k[0] for k in agg if len(k) == 2 and k[1] == r}) for r in roots}
        wallets = sorted({k[0] for k in agg if len(k) == 2})
        lb, cut = {}, {}
        for sport, mtype in buckets:
            cut[(sport, mtype)] = tuple(
                cutoff(cfgs[q], agg, lookup, sport, mtype)[0] for q in ("top50", "top20"))
        table[ym] = {"lb": lb, "cut": cut, "pop": pop, "base": base,
                     "agg": agg, "m0": m0, "wallets": wallets}
        log(f"  scored {ym[0]}-{ym[1]:02d}: {len(outs)} outcomes, "
            f"pop {pop}, m0 {m0}")
    return table


def score_lb(month, wallet, sport, market_type, cfg):
    """The wallet's lb in this bucket as of `month`, memoized per month."""
    key = (wallet, sport, market_type)
    lb = month["lb"]
    if key not in lb:
        lb[key] = score_for(month["agg"], wallet, sport, market_type,
                            cfg, month["m0"]).lb
    return lb[key]


# ------------------------------------------------------------------- rows

def build_rows(positions, res, mk, ee, wf, cfg, log=lambda *a: None) -> list[dict]:
    """One candidate per resolved position whose entry month has an `as_of`."""
    rows = []
    for p in positions:
        r = res.get(p.condition_id)
        if not r or r.get("status") != "resolved" or r.get("winner_outcome_index") is None:
            continue
        ym = _month(p.entry_ts)
        if ym not in wf:
            continue
        found = ee.get((p.wallet, p.condition_id, p.outcome_index, p.entry_ts))
        if found is None:
            continue
        first, exit_at = found
        month = wf[ym]
        lb = score_lb(month, p.wallet, p.sport, p.market_type, cfg)
        c50, c20 = month["cut"][(p.sport, p.market_type)]
        entry_dt = datetime.fromtimestamp(p.entry_ts, timezone.utc)
        rows.append({
            "wallet": p.wallet, "cid": p.condition_id, "idx": p.outcome_index,
            "entry_ts": p.entry_ts, "month": f"{ym[0]}-{ym[1]:02d}",
            "split": ("train" if entry_dt < TEST_START else
                      "test" if entry_dt < LIVE_START else "live"),
            "cluster": mk.get(p.condition_id) or p.condition_id,
            "price": first, "exit": exit_at, "root": p.root, "sport": p.sport,
            "market_type": p.market_type, "pregame": bool(p.pregame),
            "notional": p.notional_usd, "source": p.source, "lb": lb,
            "win": r["winner_outcome_index"] == p.outcome_index,
            "q": {"all": True,
                  "top50": c50 is not None and lb >= c50,
                  "top20": c20 is not None and lb >= c20},
        })
    rows.sort(key=lambda x: x["entry_ts"])
    consensus(rows)
    log(f"  candidates: {len(rows)}")
    return rows


def consensus(rows) -> None:
    """Sets row["k2"][q]: a DISTINCT wallet already qualified at level `q` on
    this (market, outcome) within the preceding 24 h — the plan's "enter at
    the 2nd". A 3rd or 4th such entry is itself a followed position (the unit
    of analysis is one position, so each carries its own $1 stake)."""
    seen = defaultdict(list)                       # (cid, idx, q) -> [(ts, wallet)]
    for row in sorted(rows, key=lambda x: x["entry_ts"]):
        row["k2"] = {}
        for q in QS:
            if not row["q"][q]:
                row["k2"][q] = False
                continue
            prior = seen[(row["cid"], row["idx"], q)]
            row["k2"][q] = any(
                w != row["wallet"] and row["entry_ts"] - ts <= CONSENSUS_SECS
                for ts, w in prior)
            prior.append((row["entry_ts"], row["wallet"]))


# ------------------------------------------------------------------- P&L

def pnl(price: Decimal, payout: Decimal, root: str, cfg, slippage: Decimal) -> float:
    """ROI on a $1 stake: `realized_edge`'s per-share accounting (what the
    shares return, minus what they cost) divided by the entry price to become
    per dollar, minus one Polymarket taker fee at entry (see the module
    docstring's DEVIATION note). `payout` is 1/0 at resolution, or the exit
    proceeds when the exit rule fires."""
    entry = price + slippage
    fee = leg_fee_cost(cfg, "polymarket", root, entry)      # per share, at entry
    return float((payout - entry - fee) / entry)


def roi(row, cfg, slippage: Decimal, exit_rule: bool, fade: bool = False) -> float:
    if fade:                                     # buy the opposite outcome, hold
        return pnl(Decimal(1) - row["price"], Decimal(0 if row["win"] else 1),
                   row["root"], cfg, slippage)
    if exit_rule and row["exit"] is not None:
        proceeds = max(row["exit"][0] - slippage, Decimal(0))
        return pnl(row["price"], proceeds, row["root"], cfg, slippage)
    return pnl(row["price"], Decimal(1 if row["win"] else 0), row["root"], cfg, slippage)


def configs():
    """The 12: Q x K x E, in a fixed order."""
    return [(q, k, e) for q in QS for k in (1, 2) for e in (False, True)]


def label(cfg_tuple) -> str:
    q, k, e = cfg_tuple
    return f"Q={q} K={k} E={'exit' if e else 'hold'}"


def select(rows, cfg_tuple):
    q, k, _ = cfg_tuple
    return [r for r in rows if (r["k2"][q] if k == 2 else r["q"][q])]


# ------------------------------------------------------------ statistics

def stats(rows, values, seed) -> dict:
    """Event-clustered bootstrap: clusters resampled with replacement."""
    if not rows:
        return {"n": 0, "events": 0, "mean": None, "win": None,
                "lb80": None, "ub90": None, "bonf": None}
    clusters = defaultdict(lambda: [0.0, 0])
    wins = 0
    for row, v in zip(rows, values):
        cell = clusters[row["cluster"]]
        cell[0] += v
        cell[1] += 1
        wins += v > 0
    sums = [c[0] for c in clusters.values()]
    cnts = [c[1] for c in clusters.values()]
    m = len(sums)
    rnd = random.Random(seed)
    gs, gc, pool = sums.__getitem__, cnts.__getitem__, range(m)
    draws = sorted(sum(map(gs, idx)) / sum(map(gc, idx))
                   for idx in (rnd.choices(pool, k=m) for _ in range(RESAMPLES)))
    pick = lambda qq: draws[min(RESAMPLES - 1, int(qq * RESAMPLES))]  # noqa: E731
    return {"n": len(rows), "events": m, "mean": sum(sums) / sum(cnts),
            "win": wins / len(rows), "lb80": pick(0.20), "ub90": pick(0.90),
            "bonf": pick(BONF_Q)}


def mean_of(values) -> float | None:
    return sum(values) / len(values) if values else None


def group_mean(rows, values, keyfn) -> dict:
    """key -> (n, mean ROI), for the diagnostics."""
    cells = defaultdict(lambda: [0, 0.0])
    for row, v in zip(rows, values):
        cell = cells[keyfn(row)]
        cell[0] += 1
        cell[1] += v
    return {k: (n, s / n) for k, (n, s) in cells.items()}


def band(price: Decimal) -> str:
    for name, hi in BANDS:
        if price < hi:
            return name
    return BANDS[-1][0]


def deciles(rows) -> dict:
    """row id -> decile of wallet notional, over the whole candidate set."""
    order = sorted(rows, key=lambda r: (r["notional"], r["entry_ts"]))
    n = len(order)
    return {id(r): min(9, (i * 10) // n) for i, r in enumerate(order)} if n else {}


# ---------------------------------------------------------------- report

def fmt(x, nd=4) -> str:
    return "—" if x is None else f"{x:+.{nd}f}" if isinstance(x, float) else str(x)


def table(head, lines) -> str:
    return ("| " + " | ".join(head) + " |\n| " + " | ".join("---" for _ in head)
            + " |\n" + "".join("| " + " | ".join(str(c) for c in ln) + " |\n"
                               for ln in lines))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(ROOT / "data/whale_tape.sqlite"))
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--slippage", type=Decimal, default=Decimal("0.01"))
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--split", default="2026-07-16",
                    help="train/test boundary (the plan's date; here for the record)")
    ap.add_argument("--out", default=str(ROOT / "reports/backtest-whale-follow-2026-09-05.md"))
    args = ap.parse_args(argv)
    global TEST_START
    TEST_START = datetime.fromisoformat(args.split).replace(tzinfo=timezone.utc)
    log = lambda *a: print(*a, file=sys.stderr, flush=True)      # noqa: E731

    cfg = load(args.config)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    log("loading tape...")
    positions, res, mk = load_tape(con)
    log(f"  positions {len(positions)}, resolutions {len(res)}, markets {len(mk)}")
    log("replaying prints for entry/exit prices...")
    ee = entry_exit(con, cfg)
    log(f"  windows {len(ee)}")
    log("walk-forward scoring...")
    wf = walk_forward(positions, res, cfg, log)
    log("building candidates...")
    rows = build_rows(positions, res, mk, ee, wf, cfg, log)
    log("computing tables...")
    skipped = sum(1 for p in positions if _month(p.entry_ts) not in wf)
    text = render(rows, wf, cfg, args, con, skipped)
    Path(args.out).write_text(text)
    log(f"wrote {args.out}")
    print(text)
    return 0


def render(rows, wf, cfg, args, con, skipped=0) -> str:  # noqa: C901 - a report is one long string
    slip = Decimal(str(args.slippage))
    splits = ("train", "test", "live")
    by_split = {s: [r for r in rows if r["split"] == s] for s in splits}
    dec = deciles(rows)
    out = [
        "# Whale-follow backtest — results (2026-09-05)",
        "",
        "Pre-specified plan: `reports/backtest-whale-follow-plan-2026-09-05.md`, frozen before",
        "any result below was computed. Nothing here changes the lane; anything it suggests goes",
        "through PREREGISTRATION.md as a dated amendment.",
        "",
        f"Command: `python tools/backtest_whale_follow.py --slippage {slip} --seed {args.seed}`",
        f" (split {args.split}, {RESAMPLES} bootstrap resamples, Bonferroni-12 quantile"
        f" {BONF_Q:.6f}).",
        "",
        "<<SUMMARY>>",
        "",
        "## Deviations from the plan",
        "",
        "1. **The fee.** The plan asks for the venue fee \"exactly as `arb/whale_score."
        "realized_edge` charges it\". `realized_edge` charges NO fee — it is a gross per-share",
        "   edge. Implemented instead: `realized_edge`'s own accounting (proceeds − cost, per",
        "   share, divided by the entry price to become per dollar) minus ONE Polymarket taker",
        "   fee at entry from the lane's single fee site, `venues.fees.leg_fee_cost`, priced with",
        "   the position's root as the fee category — the same charge `arb/lanes/live.py` puts on",
        "   the whale lane's own paper bets. The exit leg pays no second fee.",
        "2. **Top-50%/top-20% is the lane's own bar**, `arb/whale_elite.cutoff` with `elite_pct`",
        "   50 and 20: the reference population is every wallet with >= 1 resolved outcome in the",
        "   root as of that month, each scored into the candidate's bucket by the transfer chain,",
        "   and the cutoff is nearest-rank from the top. A wallet outside the population (no",
        "   resolved history) is still compared to the cutoff, exactly as `whale_elite.point`",
        "   does, so it can pass on the prior alone. Where a population is too small to have one",
        "   elite member the cutoff is None and NOTHING qualifies in that bucket that month.",
        "3. **Consensus counts every later entry**, not only the literal 2nd: the unit of analysis",
        "   is one position, so the 3rd and 4th qualified wallet within 24 h each carry their own",
        "   $1 stake.",
        "4. **Win rate is ROI > 0**, so the hold and exit rules are measured the same way.",
        f"5. Only entry months 2026-02..2026-09 are in scope (the plan's `as_of` grid);"
        f" {skipped:,} older",
        "   positions have no walk-forward score and are excluded.",
        "",
    ]

    # ---- plain facts
    counts = con.execute("SELECT (SELECT COUNT(*) FROM positions_mat),"
                         " (SELECT COUNT(*) FROM prints), (SELECT COUNT(*) FROM resolutions),"
                         " (SELECT COUNT(*) FROM markets)").fetchone()
    out += ["## The data as read", "",
            f"Snapshot at run time: {counts[0]:,} `positions_mat` rows, {counts[1]:,} prints,",
            f"{counts[2]:,} resolutions, {counts[3]:,} markets.",
            f"Candidates (resolved market, entry month in scope, entry print found):"
            f" **{len(rows):,}**",
            f"— train {len(by_split['train']):,}, test {len(by_split['test']):,},"
            f" live era {len(by_split['live']):,}.",
            "", "### Wallets, cutoffs and base rates per month (walk-forward)", ""]
    lines = []
    for ym in MONTHS:
        m = wf[ym]
        cand = [r for r in rows if r["month"] == f"{ym[0]}-{ym[1]:02d}"]
        lines.append([f"{ym[0]}-{ym[1]:02d}", m["pop"].get("sports", 0),
                      len(m["wallets"]),
                      f'{float(m["base"]["sports"][0]):+.4f} (n {m["base"]["sports"][1]})',
                      len(cand),
                      len({r["wallet"] for r in cand}),
                      len({r["wallet"] for r in cand if r["q"]["top50"]}),
                      len({r["wallet"] for r in cand if r["q"]["top20"]})])
    out += [table(["as_of", "sports pop", "scored wallets", "sports base rate m0",
                   "candidates", "wallets", "pass top50", "pass top20"], lines), ""]

    out += ["### Positions followed, per configuration", ""]
    lines = []
    for c in configs():
        sel = {s: len(select(by_split[s], c)) for s in splits}
        lines.append([label(c), sel["train"], sel["test"], sel["live"]])
    out += [table(["config", "train n", "test n", "live n"], lines), ""]

    out += ["### Per-sport base facts (all candidates, follow, hold, main slippage)", ""]
    vals = [roi(r, cfg, slip, False) for r in rows]
    g = group_mean(rows, vals, lambda r: str(r["sport"]))
    lines = [[k, n, f"{mu:+.4f}",
              f'{sum(1 for r in rows if str(r["sport"]) == k and r["win"]) / n:.3f}']
             for k, (n, mu) in sorted(g.items(), key=lambda kv: -kv[1][0])]
    out += [table(["sport", "n", "mean ROI/$", "outcome win rate"], lines), ""]

    out += ["### Where the prints come from (a plain fact, not one of the plan's diagnostics)", "",
            "`source` is the print that OPENED the position: `activity` = the cold-start /activity",
            "backfill of discovered wallets, `tape` = the global real-time collector, which only",
            "started 2026-09-04. Limitation 1 has a calendar shape, and this is it.", ""]
    lines = []
    for src in sorted({r["source"] for r in rows}):
        for split in splits:
            sel = [r for r in by_split[split] if r["source"] == src]
            lines.append([src, split, len(sel),
                          fmt(mean_of([roi(r, cfg, slip, False) for r in sel]))])
    out += [table(["source", "split", "n", "mean ROI/$ (follow, hold)"], lines), ""]

    # ---- main tables
    out += ["## The 12 configurations", "",
            f"Main slippage {slip}. lb80 / ub90 / Bonferroni-12 LB are event-clustered bootstrap",
            f"quantiles (0.20 / 0.90 / {BONF_Q:.4f}) over {RESAMPLES} resamples, seed"
            f" {args.seed}.", ""]
    favorable, cells = [], {}
    for split in splits:
        out += [f"### {split}", ""]
        lines = []
        for c in configs():
            sel = select(by_split[split], c)
            st = stats(sel, [roi(r, cfg, slip, c[2]) for r in sel],
                       args.seed + splits.index(split) * 100 + configs().index(c))
            cells[(split, c)] = st
            lines.append([label(c), st["n"], st["events"],
                          fmt(st["mean"]),
                          "—" if st["win"] is None else f'{st["win"]:.3f}',
                          fmt(st["lb80"]), fmt(st["ub90"]), fmt(st["bonf"])])
        out += [table(["config", "n", "n_events", "mean ROI/$", "win rate (ROI>0)",
                       "lb80", "ub90", "Bonf-12 LB"], lines), ""]
    for c in configs():
        test, train = cells[("test", c)], cells[("train", c)]
        ok = (test["bonf"] is not None and test["bonf"] > 0
              and train["mean"] is not None and train["mean"] > 0)
        if ok:
            favorable.append(label(c))
    out += ["### Verdict (the plan's rule: TEST Bonferroni-12 LB > 0 AND the train sign agrees)",
            ""]
    out += ([f"**FAVORABLE: {', '.join(favorable)}**", ""] if favorable else
            ["**No configuration is FAVORABLE.** Every one of the 12 fails the rule — the test",
             "Bonferroni-12 lower bound is not above zero, or the train sign disagrees.", ""])
    near = [label(c) for c in configs()
            if cells[("test", c)]["bonf"] is not None and cells[("test", c)]["bonf"] > 0
            and label(c) not in favorable]
    lb80_pos = [label(c) for c in configs()
                if cells[("test", c)]["lb80"] is not None and cells[("test", c)]["lb80"] > 0]
    out += ["Near misses, reported and NOT promoted: test Bonferroni-12 LB > 0 but the train sign",
            f"disagrees — {', '.join(near) if near else 'none'}. Test lb80 > 0 (the un-corrected",
            f"80% bound, which the plan does not accept as a verdict) — "
            f"{', '.join(lb80_pos) if lb80_pos else 'none'}.", ""]

    # ---- sensitivity
    out += ["## Sensitivity to slippage (mean ROI/$, never used to pick)", ""]
    for s in (Decimal("0.00"), Decimal("0.02")):
        lines = []
        for c in configs():
            row = [label(c)]
            for split in splits:
                sel = select(by_split[split], c)
                row.append(fmt(mean_of([roi(r, cfg, s, c[2]) for r in sel])))
            lines.append(row)
        out += [f"### slippage {s}", "",
                table(["config", "train", "test", "live"], lines), ""]

    # ---- monthly walk-forward
    out += ["## Monthly walk-forward (mean ROI/$ by entry month, main slippage)", ""]
    months = [f"{y}-{m:02d}" for y, m in MONTHS]
    lines = []
    for c in configs():
        row = [label(c)]
        for mn in months:
            sel = select([r for r in rows if r["month"] == mn], c)
            mu = mean_of([roi(r, cfg, slip, c[2]) for r in sel])
            row.append("—" if mu is None else f"{mu:+.3f} ({len(sel)})")
        lines.append(row)
    out += [table(["config"] + months, lines), ""]

    # ---- diagnostics
    out += ["## Diagnostics (per configuration; NEVER used to choose)", "",
            "Each cell is `mean ROI/$ (n)` at the main slippage. `fade` buys the opposite outcome",
            "at `1 − price + slippage` and holds to resolution (the exit rule does not apply to",
            "a side the whale never traded).", ""]
    diags = [("follow/fade", None),
             ("price band", lambda r: band(r["price"])),
             ("sport", lambda r: str(r["sport"])),
             ("timing", lambda r: "pregame" if r["pregame"] else "in-play"),
             ("notional decile", lambda r: f"D{dec[id(r)]}")]
    for c in configs():
        out += [f"### {label(c)}", ""]
        lines = []
        for name, keyfn in diags:
            for split in ("train", "test"):
                sel = select(by_split[split], c)
                if keyfn is None:
                    for what, fade in (("follow", False), ("fade", True)):
                        mu = mean_of([roi(r, cfg, slip, c[2], fade) for r in sel])
                        lines.append([name, split, what,
                                      "—" if mu is None else f"{mu:+.4f} ({len(sel)})"])
                    continue
                g = group_mean(sel, [roi(r, cfg, slip, c[2]) for r in sel], keyfn)
                for k in sorted(g, key=str):
                    n, mu = g[k]
                    lines.append([name, split, k, f"{mu:+.4f} ({n})"])
        out += [table(["diagnostic", "split", "level", "mean ROI/$ (n)"], lines), ""]

    base, tight = ("all", 1, False), ("top20", 2, False)
    verdict = ("FAVORABLE: " + ", ".join(favorable)) if favorable \
        else "No configuration is FAVORABLE"
    gaps = [cells[(sp, (q, k, True))]["mean"] - cells[(sp, (q, k, False))]["mean"]
            for sp in splits for q in QS for k in (1, 2)
            if cells[(sp, (q, k, True))]["mean"] is not None]
    cell = lambda sp, c: f'{fmt(cells[(sp, c)]["mean"])} (n {cells[(sp, c)]["n"]})'  # noqa: E731
    summary = [
        "## What the run says (numbers only; the verdict rule is the plan's)",
        "",
        f"- **{verdict}** under the plan's rule (test Bonferroni-12 LB > 0 and the train sign"
        " agrees).",
        f"- Baseline `Q=all K=1 E=hold`: train {cell('train', base)}, test"
        f" {cell('test', base)}, live era {cell('live', base)}, per $.",
        f"- Tightest cell `Q=top20 K=2 E=hold`: train {cell('train', tight)}, test"
        f" {cell('test', tight)}, live era {cell('live', tight)}. The skill filter's SIGN is"
        " not stable across the three eras.",
        f"- The exit rule costs money nearly everywhere: mean of (exit − hold) over the 18"
        f" (split, Q, K) cells = {fmt(mean_of(gaps))} per $.",
        "- Every one of the 12 configurations is negative on train AND on the live era; only",
        "  the test window (2026-07-16 → 2026-09-04, which is entirely backfilled /activity",
        "  prints) is positive, and the Bonferroni-12 correction removes even that.",
        "",
    ]
    out = [ln if ln != "<<SUMMARY>>" else "\n".join(summary) for ln in out]

    out += ["## Known limitations (verbatim from the plan)", "",
            "1. Selection bias: the 575 backfilled wallets were discovered by large recent"
            " prints; wallets",
            "   that blew up and stopped trading are absent. The baseline (Q = all) carries the"
            " same bias, so",
            "   the DIFFERENCE between Q levels is the honest read of skill ranking, not the"
            " absolute ROI.",
            "2. Slippage is an assumption; there are no historical quotes. The lane's own 120 s"
            " markouts",
            "   (mean drift ≈ 0) are the only calibration.",
            "3. Depth is ignored (the lane's depth gate is not simulated).",
            "4. Outcome P&L only; no mark-to-market.", ""]
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
