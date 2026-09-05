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
(per share, divided by the entry price to become per dollar) minus a
Polymarket taker fee from the lane's single fee site,
`arb.venues.fees.leg_fee_cost`, priced with the position's root as the fee
category — which is what `arb/lanes/live.py` charges the whale lane's own
paper bets. Revision 2: the SELL leg pays its own taker fee too
(`bid − leg_fee_cost(bid)`), as SPEC.md's exit-engine sell basis and
`arb/lanes/whale_book.exit_payload` both charge it. Resolution is not a
trade, so hold-to-resolution still pays exactly one fee, at entry.
"""
from __future__ import annotations

import argparse
import itertools
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
            "clustered_by": "event_key" if mk.get(p.condition_id) else "condition_id",
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
    """K, exactly as the plan words it: ONE stake per (market, outcome), taken
    at the time and entry price of the SECOND distinct qualified wallet within
    24 h — `row["k2first"][q]`.

    `row["k2"][q]` (a distinct wallet already qualified within the preceding
    24 h, whether or not the pair has been staked before) is kept ONLY as the
    labelled "every qualified entry" diagnostic: Revision 1 staked it as the
    config, which put up to 73 stakes on one (market, outcome) in the test
    window and so measured crowdedness, not consensus."""
    seen = defaultdict(list)                       # (cid, idx, q) -> [(ts, wallet)]
    staked = set()                                 # (cid, idx, q) already entered
    for row in sorted(rows, key=lambda x: x["entry_ts"]):
        row["k2"], row["k2first"] = {}, {}
        for q in QS:
            if not row["q"][q]:
                row["k2"][q] = row["k2first"][q] = False
                continue
            key = (row["cid"], row["idx"], q)
            prior = seen[key]
            hit = any(w != row["wallet"] and row["entry_ts"] - ts <= CONSENSUS_SECS
                      for ts, w in prior)
            row["k2"][q] = hit
            row["k2first"][q] = hit and key not in staked
            if hit:
                staked.add(key)
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
        # SPEC.md's exit-engine sell basis and whale_book.exit_payload: the
        # SELL crosses the spread AND pays the venue taker fee on the exit.
        bid = max(row["exit"][0] - slippage, Decimal(0))
        proceeds = bid - leg_fee_cost(cfg, "polymarket", row["root"], bid)
        return pnl(row["price"], proceeds, row["root"], cfg, slippage)
    return pnl(row["price"], Decimal(1 if row["win"] else 0), row["root"], cfg, slippage)


def configs():
    """The 12: Q x K x E, in a fixed order."""
    return [(q, k, e) for q in QS for k in (1, 2) for e in (False, True)]


def label(cfg_tuple) -> str:
    q, k, e = cfg_tuple
    return f"Q={q} K={k} E={'exit' if e else 'hold'}"


def select(rows, cfg_tuple, k2_key="k2first"):
    """The config's followed positions. `k2_key="k2"` is the Revision 1
    "every qualified entry" diagnostic, never a configuration."""
    q, k, _ = cfg_tuple
    return [r for r in rows if (r[k2_key][q] if k == 2 else r["q"][q])]


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


# ------------------------------------------- sample construction (Rev 2 #3)

SPLITS = ("train", "test", "live")
PREJUNE = datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp()
RECENT_BOARDS = {"DAY", "WEEK", "MONTH"}


def _split_of(ts) -> str:
    t = datetime.fromtimestamp(ts, timezone.utc)
    return "train" if t < TEST_START else "test" if t < LIVE_START else "live"


def sample_section(con, positions, res, ee, rows, cfg, slip) -> list[str]:
    """Everything that fell out between `positions_mat` and the candidate set,
    per era, plus the source mix and the honest name of the bootstrap cluster.
    Revision 1 reported only the survivors."""
    total, drop_res, drop_print = defaultdict(int), defaultdict(int), defaultdict(int)
    pre_grid = []                                    # (roi, split) before 2026-02
    for p in positions:
        s = _split_of(p.entry_ts)
        total[s] += 1
        r = res.get(p.condition_id)
        if not r or r.get("status") != "resolved" or r.get("winner_outcome_index") is None:
            drop_res[s] += 1
            continue
        found = ee.get((p.wallet, p.condition_id, p.outcome_index, p.entry_ts))
        if _month(p.entry_ts) not in MONTHS:
            if found is not None:
                pre_grid.append(pnl(found[0],
                                    Decimal(1 if r["winner_outcome_index"] == p.outcome_index
                                            else 0), p.root, cfg, slip))
            continue
        if found is None:
            drop_print[s] += 1

    by_split = {s: [r for r in rows if r["split"] == s] for s in SPLITS}
    seen, extra = set(), defaultdict(int)
    for r in rows:                                   # rows are entry_ts-sorted
        key = (r["wallet"], r["cid"], r["idx"])
        if key in seen:
            extra[r["split"]] += 1
        seen.add(key)
    ev = con.execute("SELECT COUNT(*), SUM(CASE WHEN event_key IS NULL OR event_key=''"
                     " THEN 1 ELSE 0 END) FROM markets").fetchone()

    out = ["## Sample construction (new in Revision 2)", "",
           "What fell out between `positions_mat` and the candidate set. Revision 1 printed",
           "only the survivors, so the reader could not see the censoring.", ""]
    lines = []
    for s in SPLITS:
        n = len(by_split[s])
        lines.append([s, f"{total[s]:,}", f"{drop_res[s]:,}",
                      f"{drop_res[s] / total[s]:.1%}" if total[s] else "—",
                      f"{drop_print[s]:,}", f"{n:,}",
                      f"{extra[s]:,}", f"{extra[s] / n:.1%}" if n else "—"])
    out += [table(["era", "positions_mat rows", "dropped: no resolved row", "= share",
                   "dropped: no entry print", "candidates", "extra stakes: split BUY windows",
                   "= share"], lines), ""]
    out += [
        "- **Right-censoring is the live era's story.** A position whose market has not "
        f"resolved yet has no `resolutions` row; {drop_res['live']:,} of the {total['live']:,} "
        f"live-era positions ({drop_res['live'] / max(total['live'], 1):.1%}) are in that state,",
        "  against " f"{drop_res['train'] / max(total['train'], 1):.1%} on train and "
        f"{drop_res['test'] / max(total['test'], 1):.1%} on test. The live era's mean is",
        "  therefore computed on the subset that resolved FASTEST, which is not a random",
        "  subset of it.",
        "- **Extra stakes.** One wallet + market + outcome can produce several positions when",
        "  its buying splits across the position window, and each split window is its own $1",
        "  stake. The share of candidate rows that are a 2nd-or-later window for the same",
        f"  (wallet, market, outcome) is {extra['train'] / max(len(by_split['train']), 1):.1%} "
        f"train / {extra['test'] / max(len(by_split['test']), 1):.1%} test / "
        f"{extra['live'] / max(len(by_split['live']), 1):.1%} live — so the eras are not equally",
        "  weighted per wallet-market either.",
        ""]
    if pre_grid:
        train_now = [roi(r, cfg, slip, False) for r in by_split["train"]]
        both = train_now + pre_grid
        out += [
            f"- **The 2026-02 grid start drops {len(pre_grid):,} positions**, all train-era, whose",
            f"  mean ROI/$ is {mean_of(pre_grid):+.4f} — they are the profitable tail the "
            "walk-forward",
            "  `as_of` grid cannot score. Folding them into train anyway (an illustration, not a",
            f"  configuration) moves the baseline train mean from {mean_of(train_now):+.4f} to"
            f" {mean_of(both):+.4f}:",
            "  a large move, and the sign does not change.", ""]
    lines = []
    for s in SPLITS:
        for src in sorted({r["source"] for r in by_split[s]}):
            sel = [r for r in by_split[s] if r["source"] == src]
            lines.append([s, src, f"{len(sel):,}",
                          f"{len(sel) / len(by_split[s]):.1%}",
                          f'{len({r["wallet"] for r in sel}):,}'])
    out += ["### Source mix and wallet count per era", "",
            table(["era", "source", "n", "share", "distinct wallets"], lines), "",
            "`activity` = the cold-start backfill of DISCOVERED wallets (see limitation 1);",
            "`tape` = the global real-time collector, live only from 2026-09-04. Train and test",
            "are one source; the live era is mostly the other, over a different and much wider",
            "wallet population — the three eras are not three samples of one process.", ""]

    lines = [[s, f"{sum(1 for r in by_split[s] if r['clustered_by'] == 'event_key'):,}",
              f"{sum(1 for r in by_split[s] if r['clustered_by'] == 'condition_id'):,}"]
             for s in SPLITS]
    out += ["### What the bootstrap actually clusters on", "",
            f"`markets.event_key` is empty for {ev[1]:,} of {ev[0]:,} rows, so the plan's",
            "\"event cluster\" is a **condition_id cluster** almost everywhere: the two sides of",
            "one game, and correlated markets on one event, are resampled as if independent.",
            "Every lb80 / ub90 / Bonferroni bound in this report is therefore a",
            "**condition_id-clustered** bound on train and test and a MIXED one in the live era,",
            "and is anti-conservative to the extent that markets within an event move together.", "",
            table(["era", "rows clustered by event_key", "rows clustered by condition_id"],
                  lines), ""]
    return out


def wallet_facts(con) -> dict:
    """How the wallet set came to exist — the mechanism behind limitation 1."""
    q = lambda s: con.execute(s).fetchall()               # noqa: E731
    counts = con.execute(
        "SELECT COUNT(*), SUM(source='leaderboard') FROM wallets"
        " WHERE backfilled_at IS NOT NULL").fetchone()
    boards = q("SELECT time_period, order_by, MIN(snapshot_at), MAX(snapshot_at),"
               " COUNT(DISTINCT proxy_wallet), COUNT(DISTINCT category)"
               " FROM leaderboard_snapshots GROUP BY 1, 2 ORDER BY 1")
    cats = [r[0] for r in q("SELECT DISTINCT category FROM leaderboard_snapshots")]
    per = sorted(r[0] for r in q("SELECT COUNT(*) FROM prints WHERE source='activity'"
                                 " GROUP BY proxy_wallet"))
    periods = defaultdict(set)
    for w, tp in q("SELECT DISTINCT proxy_wallet, time_period FROM leaderboard_snapshots"):
        periods[w].add(tp)
    return {"backfilled": counts[0], "from_board": counts[1] or 0, "boards": boards,
            "cats": cats, "capped": sum(1 for n in per if n >= 2000),
            "median_prints": per[len(per) // 2] if per else 0, "n_activity": len(per),
            "periods": periods,
            "first_print": {w: t for w, t in q("SELECT proxy_wallet, MIN(ts) FROM prints"
                                               " GROUP BY proxy_wallet")}}


# ------------------------------------- post-hoc cut + weekly series (Rev 2 #5)

def cohort_table(rows_by_split, cfg, slip, seed, cfgs=None) -> str:
    """mean (lb80, n) per split for each configuration in a cohort."""
    lines = []
    for i, c in enumerate(cfgs or configs()):
        line = [label(c)]
        for j, s in enumerate(SPLITS):
            sel = select(rows_by_split[s], c)
            st = stats(sel, [roi(r, cfg, slip, c[2]) for r in sel], seed + i * 10 + j)
            line.append("—" if st["mean"] is None else
                        f'{st["mean"]:+.4f} ({fmt(st["lb80"])}, n {st["n"]})')
        lines.append(line)
    return table(["config", "train mean (lb80, n)", "test mean (lb80, n)",
                  "live mean (lb80, n)"], lines)


def posthoc_section(rows, facts, cfg, slip, seed) -> list[str]:
    """NOT in the plan. Suggested by the 2026-09-05 review, diagnostic only:
    if the test-window edge is an artifact of discovering wallets by their
    recent profit, it should vanish among wallets that were already visible
    long before the test window, and among wallets that only ever made the
    ALL-time board."""
    first, periods = facts["first_print"], facts["periods"]
    pre = [r for r in rows if first.get(r["wallet"], 1 << 62) < PREJUNE]
    strata = [
        ("pre-June cohort (all)", pre),
        ("pre-June, ALL-time board ONLY", [r for r in pre
                                           if periods.get(r["wallet"]) == {"ALL"}]),
        ("pre-June, on any DAY/WEEK/MONTH board",
         [r for r in pre if periods.get(r["wallet"], set()) & RECENT_BOARDS]),
    ]
    out = ["## Post-hoc cut (not in the plan; diagnostic only)", "",
           "Suggested by the review of Revision 1, computed AFTER the plan's verdict was known,",
           "and therefore not a test of anything: it cannot promote a configuration and its",
           "bounds carry no multiplicity correction. It asks one question — does the test-window",
           "edge survive among wallets whose selection could not have been driven by profit",
           "earned inside the test window?", "",
           f"Cohort: wallets whose EARLIEST visible print predates 2026-06-01 — {len(pre):,} of",
           f"{len(rows):,} candidate rows, "
           f'{len({r["wallet"] for r in pre}):,} of {len({r["wallet"] for r in rows}):,} wallets.',
           "Strata by `leaderboard_snapshots` membership: a wallet seen ONLY on the ALL-time",
           "board was not selected for recent profit; a wallet on a DAY/WEEK/MONTH board was.", ""]
    for name, sel in strata:
        by = {s: [r for r in sel if r["split"] == s] for s in SPLITS}
        out += [f"### {name} — n {len(sel):,}, wallets "
                f'{len({r["wallet"] for r in sel}):,}'
                f' (train {len(by["train"]):,} / test {len(by["test"]):,}'
                f' / live {len(by["live"]):,})', "",
                cohort_table(by, cfg, slip, seed + 7000 + len(name)), ""]

    tw = {r["wallet"] for r in rows if r["split"] == "train"}
    te = {r["wallet"] for r in rows if r["split"] == "test"}
    ov = [r for r in rows if r["wallet"] in tw & te]
    by = {s: [r for r in ov if r["split"] == s] for s in SPLITS}
    out += ["### Overlap cohort — wallets that traded in BOTH the train and the test era", "",
            f"{len(ov):,} rows over {len(tw & te):,} wallets. The same wallets, measured twice:",
            "whatever changed between the two windows is not a change of population.", "",
            cohort_table(by, cfg, slip, seed + 8000,
                         [c for c in configs() if c[1] == 1]), ""]

    base = ("all", 1, False)
    m = lambda sel, s: mean_of([roi(r, cfg, slip, False)                    # noqa: E731
                                for r in select([x for x in sel if x["split"] == s], base)]) or 0
    n_at = lambda sel, s: len(select([x for x in sel if x["split"] == s], base))  # noqa: E731
    out += ["### What the cut says", "",
            f"On the whole sample the baseline's test mean is"
            f" {m(rows, 'test'):+.4f}. In the pre-June cohort it is",
            f"{m(pre, 'test'):+.4f} (n {n_at(pre, 'test'):,}); among the wallets that reached a"
            " DAY/WEEK/MONTH board it is",
            f"{m(strata[2][1], 'test'):+.4f} (n {n_at(strata[2][1], 'test'):,}); among the ones"
            " seen ONLY on the ALL-time board it is",
            f"{m(strata[1][1], 'test'):+.4f} — on n {n_at(strata[1][1], 'test'):,}, which is far"
            " too thin to carry a conclusion in either",
            "direction. And the overlap cohort, the same wallets measured in both windows, runs",
            f"{m(ov, 'train'):+.4f} on train against {m(ov, 'test'):+.4f} on test.", "",
            "Read together: the test-window edge does NOT survive restriction to wallets that",
            "were already visible before June, and the ALL-time-only stratum cannot arbitrate",
            "because it barely trades in the test window. Nothing here is a test and nothing",
            "here promotes a configuration.", ""]
    return out


def weekly_section(rows, cfg, slip) -> list[str]:
    weeks = defaultdict(list)
    for r in select(rows, ("all", 1, False)):
        wk = datetime.fromtimestamp(r["entry_ts"], timezone.utc).strftime("%G-W%V")
        weeks[(wk, r["split"])].append(roi(r, cfg, slip, False))
    lines = [[wk, era, f"{len(v):,}", f"{mean_of(v):+.4f}"]
             for (wk, era), v in sorted(weeks.items())]
    return ["### Weekly series of the baseline (`Q=all K=1 E=hold`), by ISO week of entry", "",
            "Where the sign flips, week by week, so the reader can see that the train/test split",
            "date is not a boundary between two regimes but a line drawn through a drifting",
            "series. A week straddling a boundary appears once per era.", "",
            table(["ISO week", "era", "n", "mean ROI/$"], lines), ""]


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
    text = render(rows, wf, cfg, args, con, skipped, positions, res, ee)
    Path(args.out).write_text(text)
    log(f"wrote {args.out}")
    print(text)
    return 0


def render(rows, wf, cfg, args, con, skipped=0, positions=(), res=None,
           ee=None) -> str:  # noqa: C901 - a report is one long string
    slip = Decimal(str(args.slippage))
    splits = SPLITS
    by_split = {s: [r for r in rows if r["split"] == s] for s in splits}
    dec = deciles(rows)
    facts = wallet_facts(con)
    conf = cfg["polymarket"]["confirmed_on"]
    fee_age = (datetime.now(timezone.utc).date() - conf).days
    out = [
        "# Whale-follow backtest — results (2026-09-05), **Revision 2**",
        "",
        "Pre-specified plan: `reports/backtest-whale-follow-plan-2026-09-05.md`, frozen before",
        "any result below was computed. Nothing here changes the lane; anything it suggests goes",
        "through PREREGISTRATION.md as a dated amendment.",
        "",
        f"Command: `python tools/backtest_whale_follow.py --slippage {slip} --seed {args.seed}`",
        f" (split {args.split}, {RESAMPLES} bootstrap resamples, Bonferroni-12 quantile"
        f" {BONF_Q:.6f}).",
        "",
        "<<CHANGELOG>>",
        "",
        "<<SUMMARY>>",
        "",
        "## Deviations from the plan",
        "",
        "1. **The fee.** The plan asks for the venue fee \"exactly as `arb/whale_score."
        "realized_edge` charges it\". `realized_edge` charges NO fee — it is a gross per-share",
        "   edge. Implemented instead: `realized_edge`'s own accounting (proceeds − cost, per",
        "   share, divided by the entry price to become per dollar) minus the Polymarket taker",
        "   fee from the lane's single fee site, `venues.fees.leg_fee_cost`, priced with",
        "   the position's root as the fee category — the same charge `arb/lanes/live.py` puts on",
        "   the whale lane's own paper bets. **Revision 2:** the SELL leg pays its own taker fee,",
        "   `bid − leg_fee_cost(bid)`, exactly as SPEC.md's exit-engine sell basis and",
        "   `arb/lanes/whale_book.exit_payload` charge it. A hold to resolution still pays exactly",
        "   one fee (resolution is not a trade); only `E=exit` pays two.",
        f"   The fee schedule's `confirmed_on` is {conf} — **{fee_age} days old against"
        f" `fee_stale_days` {cfg['fee_stale_days']}: STALE-BADGED**, not halting, per §6.5.",
        "   And the denominator: this report divides by the entry price, while the lane divides a",
        "   $1 stake by entry + fee. Every ROI/$ here is therefore about 1.8% larger in magnitude",
        "   than the lane's own accounting of the same trade (at 0.40 and the 3% sports rate,",
        "   fee/entry = 0.018); it moves no sign and no ranking.",
        "2. **Top-50%/top-20% is the lane's own bar**, `arb/whale_elite.cutoff` with `elite_pct`",
        "   50 and 20: the reference population is every wallet with >= 1 resolved outcome in the",
        "   root as of that month, each scored into the candidate's bucket by the transfer chain,",
        "   and the cutoff is nearest-rank from the top. A wallet outside the population (no",
        "   resolved history) is still compared to the cutoff, exactly as `whale_elite.point`",
        "   does, so it can pass on the prior alone. Where a population is too small to have one",
        "   elite member the cutoff is None and NOTHING qualifies in that bucket that month.",
        "3. ~~**Consensus counts every later entry**~~ — **withdrawn in Revision 2.** K=2 is now",
        "   the plan's own wording: ONE stake per (market, outcome), taken at the time and entry",
        "   price of the 2nd distinct qualified wallet within 24 h. Revision 1 staked the 3rd,",
        "   4th … entry as well, which made K=2 a measure of CROWDEDNESS, not of consensus, and",
        "   weighted the busiest markets by up to 73 stakes each. The old behaviour survives",
        "   below as the labelled \"every qualified entry\" diagnostic only.",
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

    out += sample_section(con, positions, res or {}, ee or {}, rows, cfg, slip)

    # ---- main tables
    out += ["## The 12 configurations", "",
            f"Main slippage {slip}. lb80 / ub90 / Bonferroni-12 LB are **condition_id-clustered**",
            "bootstrap quantiles (the plan said \"event cluster\"; `markets.event_key` is empty",
            "almost everywhere — see Sample construction) of"
            f" (0.20 / 0.90 / {BONF_Q:.4f}) over {RESAMPLES} resamples, seed"
            f" {args.seed}. `K=2` is one stake per (market, outcome) at the 2nd distinct",
            "qualified wallet within 24 h — the plan's rule, corrected in Revision 2.", ""]
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

    # ---- the withdrawn K=2 rule, as a labelled diagnostic
    k2rows, k2max = [], defaultdict(int)
    for r in rows:
        if r["k2"]["all"]:
            k2max[(r["cid"], r["idx"], r["split"])] += 1
    for c in [x for x in configs() if x[1] == 2]:
        line = [label(c)]
        for s in splits:
            old = select(by_split[s], c, k2_key="k2")
            new = select(by_split[s], c)
            line += [f'{fmt(mean_of([roi(r, cfg, slip, c[2]) for r in old]))} ({len(old):,})',
                     f'{fmt(mean_of([roi(r, cfg, slip, c[2]) for r in new]))} ({len(new):,})']
        k2rows.append(line)
    worst = {s: max([v for (_, _, sp), v in k2max.items() if sp == s] or [0]) for s in splits}
    out += ["### Diagnostic: the withdrawn K=2 rule (\"every qualified entry\")", "",
            "Revision 1's K=2 staked the 3rd, 4th … qualified wallet as well. Its worst single",
            f'(market, outcome) carried {worst["train"]} stakes on train, {worst["test"]} on test'
            f' and {worst["live"]} in the live era,',
            "so its mean was a crowdedness-weighted average of a few busy markets. Reported here",
            "for continuity with Revision 1; it is NOT one of the 12 configurations and it enters",
            "no verdict.", "",
            table(["config", "train old (n)", "train plan (n)", "test old (n)", "test plan (n)",
                   "live old (n)", "live plan (n)"], k2rows), ""]

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

    out += posthoc_section(rows, facts, cfg, slip, args.seed)
    out += weekly_section(rows, cfg, slip)

    base, tight = ("all", 1, False), ("top20", 2, False)
    verdict = ("FAVORABLE: " + ", ".join(favorable)) if favorable \
        else "No configuration is FAVORABLE"
    gaps = [cells[(sp, (q, k, True))]["mean"] - cells[(sp, (q, k, False))]["mean"]
            for sp in splits for q in QS for k in (1, 2)
            if cells[(sp, (q, k, True))]["mean"] is not None]
    cell = lambda sp, c: f'{fmt(cells[(sp, c)]["mean"])} (n {cells[(sp, c)]["n"]})'  # noqa: E731
    pos = {sp: sum(1 for c in configs()
                   if (cells[(sp, c)]["mean"] or 0) > 0) for sp in splits}
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
        f" (split, Q, K) cells = {fmt(mean_of(gaps))} per $ — and it now pays the taker fee on",
        "  the sell, which is part of why.",
        f"- Positive cells, out of 12: train {pos['train']}, test {pos['test']}, live era"
        f" {pos['live']}. The test window (2026-07-16 →",
        "  2026-09-04, entirely backfilled /activity prints) holds nearly all of them, and the",
        "  Bonferroni-12 correction removes even those.",
        "- The test window is also the window the wallet set was SELECTED in (limitation 1) and",
        "  the one whose markets have all resolved (Sample construction). Both point the same",
        "  way, and the post-hoc cut below is the attempt to separate them.",
        "",
    ]
    changelog = [
        "## Changelog — Revision 2 (2026-09-05)",
        "",
        "Revision 1 (commit `aebfa6c`) was reviewed; the verdict survived, the test-window",
        "numbers did not. Each correction below and what it did to the numbers:",
        "",
        "1. **K=2 is now the plan's rule** — one stake per (market, outcome) at the 2nd distinct",
        "   qualified wallet, not one per qualified wallet. *Effect:* every K=2 cell changes; the",
        f"   headline `Q=all K=2 E=hold` test mean goes from"
        f" {fmt(mean_of([roi(r, cfg, slip, False) for r in select(by_split['test'], ('all', 2, False), k2_key='k2')]))}"
        f" (Revision 1) to {cell('test', ('all', 2, False))},",
        "   and its n falls with it. The withdrawn rule is kept as a labelled diagnostic.",
        "2. **The exit leg pays the venue taker fee** (SPEC.md's sell basis;",
        "   `whale_book.exit_payload`). *Effect:* every `E=exit` cell falls by roughly the",
        "   Polymarket fee on the exit price; the exit rule was already the worse of the two and",
        "   is now worse by more. No hold cell moves.",
        "3. **New \"Sample construction\" section** — per-era censoring, the pre-2026-02 drop and",
        "   what it would do to train, split-window extra stakes, source mix, and the fact that",
        "   the bootstrap clusters on condition_id, not event. *Effect:* no number changes; the",
        "   bounds are relabelled honestly and the live era's right-censoring is now visible.",
        "4. **Limitation 1 rewritten to the actual mechanism** — the wallet set is a leaderboard",
        "   sample, and two of the four boards look back INTO the test window. *Effect:* no",
        "   number changes; the reading of the test window does.",
        "5. **New post-hoc cut** (labelled, not in the plan): the 12-config grid on wallets first",
        "   seen before 2026-06-01, split by leaderboard membership, the train/test overlap",
        "   cohort, and a weekly series of the baseline. *Effect:* adds evidence, promotes",
        "   nothing.",
        "6. **The fee schedule is stale-badged** and the ROI denominator is stated against the",
        f"   lane's (entry, not entry + fee: ≈1.8% of magnitude). *Effect:* no number changes.",
        "",
    ]
    subst = {"<<SUMMARY>>": "\n".join(summary), "<<CHANGELOG>>": "\n".join(changelog)}
    out = [subst.get(ln, ln) for ln in out]

    look = {"DAY": 1, "WEEK": 7, "MONTH": 30}       # the board's own P&L window
    boards = []
    for tp, ob, lo, hi, c, _ in facts["boards"]:
        span = ""
        if tp in look:
            end = datetime.fromisoformat(hi).date()
            span = (f", so its P&L window covers"
                    f" {end - timedelta(days=look[tp])}→{end}")
        boards.append(f"**{tp}** (order_by {ob}, {c:,} wallets, snapshotted"
                      f" {lo[:10]}→{hi[:10]}{span})")
    boards = "; ".join(boards)
    out += ["## Known limitations", "",
            "Limitation 1 is REWRITTEN in Revision 2 — the plan's wording (\"discovered by large",
            "recent prints\") named the wrong mechanism. 2–4 are verbatim from the plan.", "",
            f"1. **Selection on profit earned inside the test window.** {facts['from_board']:,} of"
            f" the {facts['backfilled']:,}",
            f"   backfilled wallets have `wallets.source = 'leaderboard'`: they came from"
            f" `leaderboard_snapshots`,",
            f"   category {'/'.join(facts['cats'])}, ordered by PNL — {boards}.",
            "   The MONTH board looks back ~30 days and the WEEK board ~7 from a snapshot taken",
            "   2026-09-03..05, so both windows lie INSIDE the test window (2026-07-16 →",
            "   2026-09-04). The sample is therefore conditioned on RECENT PROFIT, measured in",
            "   the period the test window measures. That is the mechanism, and it points the",
            "   same way as the result: the test window is the era that comes out positive.",
            f"   Compounding it, the /activity backfill caps at 2,000 prints per wallet —"
            f" {facts['capped']} of",
            f"   {facts['n_activity']} wallets sit at the cap and the median wallet has"
            f" {facts['median_prints']:,} prints — so for a",
            "   high-frequency wallet the \"first visible print\" is recent by construction, and",
            "   its early, possibly worse, history is simply not in the tape.",
            "   Wallets that blew up and stopped trading are absent either way. The baseline",
            "   (Q = all) carries the same bias, so the DIFFERENCE between Q levels is the",
            "   honest read of skill ranking, not the absolute ROI.",
            "2. Slippage is an assumption; there are no historical quotes. The lane's own 120 s"
            " markouts",
            "   (mean drift ≈ 0) are the only calibration.",
            "3. Depth is ignored (the lane's depth gate is not simulated).",
            "4. Outcome P&L only; no mark-to-market.", ""]
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
