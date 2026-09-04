"""L14 read-time trader profiles: rank every scored wallet, and say WHERE it
is good (design §12 "Trader profiles (read-time)").

DIAGNOSTICS ONLY. Nothing in this module feeds qualification, points, sizing,
exits or firing — V1..V4 and `whale.min_score_lb` are untouched. It reads the
`WhaleState` the lane and the daily report already built and re-derives nothing
from the tape: same positions, same resolutions, same base rates, same
`whale_score` predicate for "is this position scorable yet".

Every wallet is profiled, hygiene-excluded ones included: they are FLAGGED, not
hidden, because "which of my whales is actually a market maker" is exactly the
question this table exists to answer.

Decision (controller, 2026-09-04): every statistic in a profile row —
win_rate, implied, excess, edge, roi, t_stat, lb80 — is computed over CLEAN
positions only (`state.open_positions`, exactly what the lane's own score
uses), so the table's lb80 is the lb80 the lane acts on. Flagged positions
contribute only to `flagged_n` (`n = clean_n + flagged_n` stays visible as
`clean_n/n`). A wholly excluded wallet ("full", clean_n = 0) shows its stats
over its OWN flagged positions instead — never blanked — so a market maker is
still ranked and visible, never hidden.

Rule 4: `whale.rank.min_n` (display floor) and `whale.rank.top` are the only
        numbers here; every model constant comes from `whale.score`.
Rule 5: prices, edges and money are Decimal, quantized q6. The posterior and
        the t-statistic run in float — statistical internals, per rule 5's
        second sentence — and are re-quantized immediately.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from decimal import Decimal

from arb import whale_score
from arb.models import q6

# How far down the root -> sport -> bucket chain a row at each level runs.
# `whale_score.posterior` zips its `levels` argument against `LEVELS`, so a
# truncated chain IS that model's posterior at that level: no second model, no
# new prior, nothing added to whale_score.
DEPTH = {"root": 1, "sport": 2, "bucket": 3}

# Sort sentinel for a null lb80/t_stat. Never displayed — `_order` is only ever
# a sort key, and a null still ranks (last), it is not dropped.
_LAST = Decimal("-1e9")

HEADER = (f"{'wallet':<14}{'level':<7}{'key':<26}{'n':>4} {'win%':>6} "
          f"{'impl%':>6} {'excess%':>8} {'edge':>9} {'roi@risk':>10} "
          f"{'t':>10} {'lb80':>10} {'notional':>12}  flag")


def _blank():
    return {"n": 0, "wins": 0, "size": Decimal(0), "cost": Decimal(0),
            "gain": Decimal(0), "notional": Decimal(0), "e": [], "last": None,
            "clean_n": 0, "flagged_n": 0, "root": None}


def profiles(state, cfg, as_of) -> list[dict]:
    """One row per (wallet, level, key), level in root / sport / bucket.

    Point-in-time: a position counts only once `whale_score.resolved_outcomes`
    says it is scorable as of `as_of` — the ONE scorability predicate, called
    here rather than restated, so a profile can never count a position the
    score cannot see.

    Every descriptive stat and `agg` (which feeds `_chain`'s posterior, hence
    lb80) come from CLEAN positions only — same population as `state.lookup`
    — with a per-key fallback to a wallet's own flagged positions wherever it
    has no clean contribution at that key at all (module docstring, 2026-09-04
    decision). `n`/`clean_n`/`flagged_n` still count every position, clean or
    not: that bookkeeping is the whole point of the tri-state `hygiene` flag.
    """
    open_ids = {id(p) for p in state.open_positions}
    cells, flag_only = defaultdict(_blank), defaultdict(_blank)
    clean_outcomes, flagged_outcomes = [], []
    for p in state.positions:
        got = whale_score.resolved_outcomes([p], state.resolutions, as_of)
        if not got:
            continue
        o = got[0]
        clean = id(p) in open_ids
        (clean_outcomes if clean else flagged_outcomes).append(o)
        # "win" is the payout's own definition: 1 on the side actually held.
        won = (state.resolutions[p.condition_id].get("winner_outcome_index")
               == p.outcome_index)
        for level, key in (("root", p.root), ("sport", p.sport), ("bucket", p.bucket)):
            c = cells[(p.wallet, level, key)]
            c["root"] = p.root
            c["n"] += 1
            if clean:
                c["clean_n"] += 1
            else:
                c["flagged_n"] += 1
            # the clean population accumulates straight into the cell; a
            # flagged position accumulates into its own shadow cell instead,
            # so it can never blend into a partial cell's stats — only stand
            # in, wholesale, for a wholly-excluded one (below).
            stat = c if clean else flag_only[(p.wallet, level, key)]
            stat["wins"] += int(won)
            stat["size"] += p.size
            stat["cost"] += p.avg_price * p.size
            # `o.e` is realized edge per share, which IS payout - avg_price for
            # a position held to resolution and the sell-aware generalisation
            # when part of it was sold first (whale_score.realized_edge).
            stat["gain"] += o.e * p.size
            stat["notional"] += p.notional_usd
            stat["e"].append(o.e)
            stat["last"] = max(stat["last"] or o.resolved_at, o.resolved_at)
    # clean-only agg matches the lane's own score; a key with zero clean
    # contribution (a wholly-excluded wallet's own keys) falls back to that
    # wallet's flagged outcomes instead of going dark. dict.update semantics:
    # applying the clean pass second replaces any flagged entry at the same
    # key wholesale, never blends the two.
    agg = {**whale_score.aggregate(flagged_outcomes), **whale_score.aggregate(clean_outcomes)}
    for key, c in cells.items():
        if c["clean_n"] == 0:
            fo = flag_only.get(key)
            if fo:
                for field in ("wins", "size", "cost", "gain", "notional", "e", "last"):
                    c[field] = fo[field]
    return [_row(wallet, level, key, cell, agg, state, cfg)
            for (wallet, level, key), cell in cells.items()]


def _mean(cell) -> tuple[int, float]:
    n, total = cell
    return n, (float(total) / n if n else 0.0)


def _sport_of(level, key) -> str | None:
    """The sport a row sits under — the key itself at sport level, the bucket's
    own prefix at bucket level, and nothing at root level (a root row is every
    sport at once)."""
    return key if level == "sport" else key.split(":", 1)[0] if level == "bucket" else None


def _chain(agg, wallet, root, level, key) -> list[tuple[int, float]]:
    """The (n, mean e) pairs `whale_score.posterior` wants, truncated to this
    row's level. The keys are `whale_score._agg_keys`' own, root first, so a
    sum can no more cross the sports/nonsports line here than it can there."""
    sport = _sport_of(level, key)
    keys = [(wallet, root), (wallet, root, sport), (wallet, root, sport, key)]
    return [_mean(agg.get(k, (0, Decimal(0)))) for k in keys[:DEPTH[level]]]


def _row(wallet, level, key, c, agg, state, cfg) -> dict:
    n, root = c["n"], c["root"]
    # the population the STATS below are actually drawn from: clean when any,
    # else (a wholly-excluded "full" cell) the wallet's own flagged positions
    # — never the displayed `n`, which stays clean_n + flagged_n regardless.
    stat_n = c["clean_n"] or c["flagged_n"]
    s = cfg["whale"]["score"]
    m0 = state.rates.get(root, Decimal(str(s["base_rate_default"])))
    mu, var = whale_score.posterior(cfg, m0, _chain(agg, wallet, root, level, key))
    # sample sd is undefined at n = 1; 0 keeps the column typed and t_stat null
    sd = statistics.stdev([float(e) for e in c["e"]]) if stat_n >= 2 else 0.0
    raw_mean = sum(c["e"], Decimal(0)) / stat_n
    edge_mean = q6(raw_mean)
    win_rate, implied = q6(Decimal(c["wins"]) / stat_n), q6(c["cost"] / c["size"])
    # Three-valued, not one bool (review of ecb7e8a): "" no exclusion in this
    # cell, "partial" some positions excluded and some not (a single wallet
    # market maker looks identical to a mostly-clean wallet with one bad
    # market unless the two counts are kept apart), "full" every position in
    # the cell is hygiene-excluded.
    hygiene = "" if c["flagged_n"] == 0 else ("full" if c["clean_n"] == 0 else "partial")
    return {
        "wallet": wallet, "level": level, "key": key, "root": root,
        "sport": _sport_of(level, key),
        "n": n, "wins": c["wins"], "win_rate": win_rate, "implied": implied,
        "excess_win_rate": q6(win_rate - implied),
        "edge_mean": edge_mean, "edge_sd": q6(Decimal(sd)),
        # dollar-weighted return on the capital actually put at risk; a
        # zero-cost book has no denominator, which is a null, not a zero
        "roi_at_risk": q6(c["gain"] / c["cost"]) if c["cost"] > 0 else None,
        "t_stat": q6(Decimal(float(raw_mean) / (sd / math.sqrt(stat_n)))) if sd > 0 else None,
        "lb80": q6(Decimal(mu - float(s["z"]) * math.sqrt(var))),
        "excess_vs_base": q6(edge_mean - m0),
        "notional": q6(c["notional"]),
        "last_resolved_at": c["last"].isoformat(),
        # a bucket row is the wallet's own record in that market type; sport
        # and root rows are aggregates over several
        "native": level == "bucket",
        # clean_n: positions the lane's own score used (state.open_positions).
        # flagged_n: hygiene-excluded ones counted into this same cell's `n`.
        "clean_n": c["clean_n"], "flagged_n": c["flagged_n"], "hygiene": hygiene,
        "hygiene_flag": c["flagged_n"] > 0}   # kept for compatibility


# ---------------------------------------------------------------- ordering

def _order(r) -> tuple:
    return (r["lb80"] if r["lb80"] is not None else _LAST,
            r["t_stat"] if r["t_stat"] is not None else _LAST)


def _sorted(rows) -> list[dict]:
    """lb80 desc, then t_stat desc, then (wallet, key) — a stable pre-sort on
    the names, so two identical scores always print in the same order and a
    day's report re-runs to the same bytes (rule 9)."""
    rows = sorted(rows, key=lambda r: (r["wallet"], r["key"]))
    rows.sort(key=_order, reverse=True)
    return rows


def rank(profiles, cfg, level="bucket", min_n=None) -> list[dict]:
    """The display table at one level. `min_n` (whale.rank.min_n) is a DISPLAY
    floor only: a row below it was still computed and is still in `profiles`."""
    floor = int(cfg["whale"]["rank"]["min_n"] if min_n is None else min_n)
    return _sorted([r for r in profiles
                    if r["level"] == level and r["n"] >= floor])


def best_markets(profiles, wallet, k, min_n=0) -> list[dict]:
    """Where this wallet is good: its top-k buckets by lb80. `min_n` defaults
    to no floor; callers that display pass `whale.rank.min_n`."""
    return _sorted([r for r in profiles if r["wallet"] == wallet
                    and r["level"] == "bucket" and r["n"] >= int(min_n)])[:k]


def wallet_profile(profiles, wallet) -> list[dict]:
    """One wallet's whole record: root, then sport, then bucket, best first."""
    rows = _sorted([r for r in profiles if r["wallet"] == wallet])
    rows.sort(key=lambda r: DEPTH[r["level"]])
    return rows


# ------------------------------------------------------------------ render

def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _n(v) -> str:
    return "—" if v is None else str(v)


def _n_ratio(r) -> str:
    """`clean_n/n` once anything in the cell is flagged, else plain `n` — the
    ratio is the whole point (a cell can be `partial` with most of it clean)."""
    return f"{r['clean_n']}/{r['n']}" if r["flagged_n"] else str(r["n"])


def line(r) -> str:
    """One `arb whale-rank` row, aligned under HEADER."""
    return (f"{r['wallet'][:13]:<14}{r['level']:<7}{r['key'][:25]:<26}"
            f"{_n_ratio(r):>4} {_pct(r['win_rate']):>6} {_pct(r['implied']):>6} "
            f"{_pct(r['excess_win_rate']):>8} {r['edge_mean']:>9} "
            f"{_n(r['roi_at_risk']):>10} {_n(r['t_stat']):>10} {r['lb80']:>10} "
            f"{r['notional']:>12}  {r['hygiene']}")


def traders_frame(cfg, state, as_of) -> dict:
    """What the daily report and the dashboard print: the ranked bucket table
    and, per top wallet, where it is good. Diagnostics, never an input."""
    if state is None:
        return {"top": [], "best": {}}
    rows = profiles(state, cfg, as_of)
    floor = int(cfg["whale"]["rank"]["min_n"])
    top = rank(rows, cfg)[:int(cfg["whale"]["rank"]["top"])]
    # 5 wallets x 3 buckets is the section's layout, like `_qualifying`'s
    # top-20 roster — not a threshold, and nothing is filtered out by it
    return {"top": top,
            "best": {w: best_markets(rows, w, 3, floor)
                     for w in dict.fromkeys(r["wallet"] for r in top[:5])}}


def traders_md(frame: dict) -> list[str]:
    """The daily report's "Top traders" section (design §12). Read-time
    diagnostics over the tape's resolved history — NOT the lane's P&L, and not
    an input to any fire rule."""
    md = ["",
          "**Top traders (read-time diagnostics, NOT a lane input).** Ranked by "
          "lb80 at bucket level over the tape's resolved history. `excess%` is the "
          "win rate above the break-even the price itself implied — success above "
          "average given the risk taken. Hygiene-excluded wallets are ranked and "
          "flagged, never hidden.",
          "", "| trader | bucket | n | win% | implied% | excess% | edge pts | "
          "ROI at risk | t | lb80 | flag |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in frame["top"]:
        md.append(f"| {r['wallet']} | {r['key']} | {_n_ratio(r)} | {_pct(r['win_rate'])} | "
                  f"{_pct(r['implied'])} | {_pct(r['excess_win_rate'])} | "
                  f"{r['edge_mean']} | {_n(r['roi_at_risk'])} | {_n(r['t_stat'])} | "
                  f"{r['lb80']} | {r['hygiene'] or '—'} |")
    if not frame["top"]:
        md.append("| — | — | 0 | — | — | — | — | — | — | — | — |")
    md.append("")
    for wallet, best in frame["best"].items():
        where = ", ".join(f"{b['key']} (lb80 {b['lb80']}, n {b['n']}, excess "
                          f"{_pct(b['excess_win_rate'])})" for b in best)
        md.append(f"- where **{wallet}** is good: {where or 'no bucket at the display floor'}")
    return md
