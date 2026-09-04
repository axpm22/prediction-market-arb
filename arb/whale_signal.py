"""L14 signal aggregation, the fire rule and the confidence stake (§7, §8).

PURE. No sqlite, no network, no clock: `now` is always an argument, scores
arrive through a `score_lookup(wallet, sport, market_type) -> Score | None`
callable so this module never sees Task 5's aggregation internals, and the
`open_positions` handed in are already hygiene-filtered by the caller.

Rule 4: every threshold comes from `cfg["whale"]`, except the depth floor,
        which is the one global `cfg["min_executable_usd"]`.
Rule 5: points, ratios, VWAPs and stakes are Decimal, quantized with q6 at
        the outputs. The only float is `math.log10` inside V3 (a statistical
        weight), re-quantized on the same line.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from arb import whale_dominance, whale_elite
from arb.models import q6
from arb.whale_merge import d, merge_windows, qualifying, ts

VARIANTS = ("V1", "V2", "V3", "V4", "V5", "V6")
YES, NO = 0, 1


@dataclass(frozen=True)
class Touch:
    """One side's book at signal time. `ask`/`bid` are None when that half of
    the book is empty; `depth_usd` is the size at the ask (spec §7.5)."""
    ask: Decimal | None
    bid: Decimal | None
    depth_usd: Decimal


@dataclass(frozen=True)
class Signal:
    """One (market, variant) evaluation. Persisted whether it fired or not:
    suppressed signals are this lane's "left on the table" (§7)."""
    variant: str
    side: int
    points_yes: Decimal
    points_no: Decimal
    ratio: Decimal
    whales: list[dict]        # both sides, sybil-dropped included and flagged
    vwap: Decimal
    touch_ask: Decimal | None
    touch_bid: Decimal | None
    depth_usd: Decimal
    fired: bool
    suppressed_reason: str | None
    flagged_edge: Decimal
    S: Decimal
    holders_ratio: Decimal | None = None   # §7 item 1b; None = nobody holds


# -------------------------------------------------------------------- points

def point_of(variant, whale, cfg, ranked=frozenset(), ctx=None) -> Decimal:
    """Spec §7's variant table for one whale dict (`lb`, `open_notional`).

    `ranked` is the latest leaderboard snapshot's wallets (§7, amended
    2026-09-04). V4 is a FILTER on top of the same qualification bar every
    other variant uses, never a score input: a whale is only in `whales` at
    all because `qualifying` cleared it on our own realized-edge scores, and
    rank then decides whether it carries its one vote.

    `ctx` is the cycle's per-market context: V6's market facts (`volume`,
    `now_ts`, `ask` per side — arb/whale_dominance.py) and V5's `elite` cutoff
    for this bucket (arb/whale_elite.py). Every key is optional and each
    variant reads only its own."""
    if variant == "V1":
        return Decimal(1)
    if variant == "V4":
        return Decimal(1) if whale["wallet"] in ranked else Decimal(0)
    if variant == "V5":
        return whale_elite.point(whale, ctx)
    if variant == "V6":
        return whale_dominance.point(whale, cfg, ctx)
    excess = d(whale["lb"]) - d(cfg["whale"]["min_score_lb"])
    if variant == "V2":
        return q6(excess)
    unit = d(cfg["whale"]["size"]["unit_usd"])
    weight = d(math.log10(1 + float(d(whale["open_notional"]) / unit)))
    return q6(excess * weight)


def points(variant, whales_on_side, cfg, ranked=frozenset(), ctx=None) -> Decimal:
    """One side's total. Sybil-dropped whales contribute nothing, so this
    agrees with `Signal.points_yes/points_no` — but the caller must hand in ONE
    side: `Signal.whales` carries both, each with its own `side`."""
    return q6(sum((point_of(variant, w, cfg, ranked, ctx) for w in whales_on_side
                   if not w.get("sybil_dropped")), Decimal(0)))


def wmean(whales, key) -> Decimal:
    """Points-weighted mean of `key`. A whale whose score fell below the bar
    can carry negative points, so weights clamp at zero and can never flip the
    mean's sign; with no positive weight left the plain mean stands."""
    weights = [max(d(w["points"]), Decimal(0)) for w in whales]
    total = sum(weights, Decimal(0))
    if total <= 0:
        return q6(sum((d(w[key]) for w in whales), Decimal(0)) / len(whales))
    return q6(sum((d(w[key]) * x for w, x in zip(whales, weights)), Decimal(0)) / total)


# -------------------------------------------------------------- qualification

def sybil(whales, gap) -> None:
    """Spec §7.3: same-side prints within `gap` seconds are one whale.

    Single linkage on `last_ts`: sort by time, cut wherever the step from the
    previous print exceeds `gap`. A chain A-B-C, each within `gap` of its
    neighbour but not of the far end, is therefore ONE whale, and the answer
    depends only on the timestamps — the earlier greedy pass against a kept set
    left 1 or 2 survivors for the same three prints depending on which held the
    top `lb`. Under `taker_only: false` (2026-09-04) two makers filling the
    same side of one taker's order share a timestamp and therefore collapse to
    one whale here: one fill is one piece of information, so counting it once
    is the conservative reading, and it is stated rather than silent.
    Highest `lb` in a cluster survives (ties to the lowest wallet);
    the rest stay in the row, flagged, so the heuristic is auditable rather
    than silent. Idempotent: every row's flag is (re)set from its cluster.
    """
    clusters: list[list[dict]] = []
    for x in sorted(whales, key=lambda x: (x["last_ts"], x["wallet"])):
        if clusters and x["last_ts"] - clusters[-1][-1]["last_ts"] <= gap:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    for c in clusters:
        keep = min(c, key=lambda x: (-x["lb"], x["wallet"]))
        for x in c:
            x["sybil_dropped"] = x is not keep


def tally(variant, whales, cfg, ranked=frozenset(), ctx=None) -> tuple[dict, dict]:
    """Side split -> sybil collapse -> points -> per-side totals: the one
    computation `evaluate` and `exit_reason` both run. `whales` is a
    `qualifying` list carrying both sides; the returned `scored` rows are
    copies carrying `points` (zero for the sybil-dropped), keyed by side."""
    gap = int(cfg["whale"]["fire"]["sybil_gap_secs"])
    scored = {}
    for s in (YES, NO):
        side = [x for x in whales if x["side"] == s]
        sybil(side, gap)
        scored[s] = [dict(x, points=(Decimal(0) if x["sybil_dropped"]
                                     else point_of(variant, x, cfg, ranked, ctx)))
                     for x in side]
    return ({s: q6(sum((x["points"] for x in ws), Decimal(0)))
             for s, ws in scored.items()}, scored)


# ------------------------------------------------------- holders (§7 item 1b)

def holders_share(variant, condition_id, open_positions, score_lookup, side,
                  now, cfg, ranked=frozenset(), ctx=None) -> tuple:
    """(`side`'s share, its points, both sides' points) over the whales still
    HOLDING this market — `qualifying(..., fresh_only=False)`, every qualifying
    wallet's merged net pregame position whatever the age of the print that
    opened it — tallied under the caller's own variant.

    ONE implementation, two callers (L14 amendment 2026-09-04): §7's entry gate
    1b and §9's `flip` exit ask the same question of the same tally, so they
    can never drift. A share of `None` — nobody qualifying holds this market —
    means the same thing in both: no reading at all, never a zero share.
    """
    holding = qualifying(condition_id, open_positions, score_lookup, now, cfg,
                         fresh_only=False)
    by_side, _ = tally(variant, holding, cfg, ranked, ctx)
    total = by_side[YES] + by_side[NO]
    return ((q6(by_side[side] / total) if total > 0 else None),
            by_side[side], total)


# ------------------------------------------------------------------ evaluate

def evaluate(market, open_positions, score_lookup, touch, now, cfg, m0,
             ranked=frozenset(), volume=None, elite=None) -> list[Signal]:
    """The pre-registered variants per market (§7; V4, V5 and V6 all added
    2026-09-04). Empty when no whale qualifies on either side — nothing to
    persist, nothing left on the table. `ranked` is V4's leaderboard filter,
    `volume` the market's own 24h venue volume for V6's denominator (None =
    unknown, read as `dominance.min_volume_usd`), `elite` V5's percentile
    cutoff for this market's bucket (None = no population, so nobody is
    elite); each is ignored by every other variant."""
    whales = qualifying(market["condition_id"], open_positions, score_lookup, now, cfg)
    if not whales:
        return []
    ctx = {"volume": volume, "now_ts": ts(now), "elite": elite,
           "ask": {s: t.ask for s, t in touch.items()}}
    return [_signal(v, market, whales, open_positions, score_lookup, touch,
                    now, cfg, m0, ranked, ctx) for v in VARIANTS]


def _signal(variant, market, whales, open_positions, score_lookup, touch, now,
            cfg, m0, ranked, ctx=None) -> Signal:
    w, f = cfg["whale"], cfg["whale"]["fire"]
    total, scored = tally(variant, whales, cfg, ranked, ctx)
    side = YES if total[YES] >= total[NO] else NO      # a tie fails `ratio` below
    denom = total[YES] + total[NO]
    ratio = q6(total[side] / denom) if denom > 0 else Decimal(0)

    live = [x for x in scored[side] if not x["sybil_dropped"]]
    vwap = wmean(live, "avg_price") if live else Decimal(0)
    fresh_vwap = wmean(live, "fresh_avg_price") if live else Decimal(0)
    S = wmean(live, "lb") if live else Decimal(0)
    t = touch.get(side)
    ask, bid = (t.ask, t.bid) if t else (None, None)
    depth = t.depth_usd if t else Decimal(0)
    threshold = f["min_points"][variant]
    age = ts(now) - max((x["last_ts"] for x in live), default=ts(now))
    to_start = (ts(market.get("start_utc")) or 0) - ts(now)
    # §7 item 1b (L14 amendment 2026-09-04): condition 1 asks whether the
    # whales just moved; this asks whether the whales actually IN this market
    # agree. Always computed, so the share is on every row for the report even
    # when an earlier gate is the one reported.
    held, _, _ = holders_share(variant, market["condition_id"], open_positions,
                               score_lookup, side, now, cfg, ranked, ctx)

    if ratio < d(f["ratio"]):
        reason = "ratio"
    elif threshold is not None and total[side] < d(threshold):
        reason = "min-points"
    elif threshold is None:                            # §7.2 calibration window
        reason = "uncalibrated"
    elif held is not None and held < d(f["holders_ratio"]):
        reason = "holders"
    elif age >= int(w["max_print_age_secs"]):          # §13: strictly younger
        reason = "late-print"
    elif ask is not None and ask > fresh_vwap + d(f["max_drift_points"]):
        reason = "stale"
    elif market.get("start_utc") is None or to_start < int(f["min_secs_to_start"]):
        reason = "too-close"
    elif ask is not None and depth < d(cfg["min_executable_usd"]):
        reason = "depth"
    elif ask is None or bid is None:
        reason = "one-sided"
    else:
        reason = None
    return Signal(variant=variant, side=side, points_yes=total[YES],
                  points_no=total[NO], ratio=ratio,
                  whales=scored[YES] + scored[NO], vwap=vwap, touch_ask=ask,
                  touch_bid=bid, depth_usd=depth, fired=reason is None,
                  suppressed_reason=reason, flagged_edge=q6(S - d(m0)), S=S,
                  holders_ratio=held)


# --------------------------------------------------------------- calibration

def calibrate_min_points(signal_rows, cfg) -> dict | None:
    """Spec §7.2, run once at the end of the calibration window: one value per
    distinct wallet that qualified in any evaluated signal (its median, since a
    wallet's score moves across the window), then `V2 = 3 * q10` and `V3` the
    same scaled by the median open notional's size weight. None below
    `calibration_min_whales` — the caller extends the window by 7 days."""
    fire, unit = cfg["whale"]["fire"], d(cfg["whale"]["size"]["unit_usd"])
    bar = d(cfg["whale"]["min_score_lb"])
    per_wallet: dict[str, list[list[Decimal]]] = {}
    for row in signal_rows:
        for x in row["whales"]:
            cell = per_wallet.setdefault(x["wallet"], [[], []])
            cell[0].append(d(x["lb"]) - bar)
            cell[1].append(d(x["open_notional"]))
    if len(per_wallet) < int(fire["calibration_min_whales"]):
        return None
    excess = sorted(median(lbs) for lbs, _ in per_wallet.values())
    notional = median([median(ns) for _, ns in per_wallet.values()])
    q10 = excess[max(math.ceil(Decimal("0.10") * len(excess)) - 1, 0)]  # nearest rank
    v2 = q6(3 * q10)
    weight = d(math.log10(1 + float(notional / unit)))
    return {"q10": q6(q10), "V2": v2, "V3": q6(3 * q10 * weight),
            "n_whales": len(per_wallet), "median_open_notional": q6(notional)}


# ---------------------------------------------------------------- conf stake

def conf_stake(S, cfg) -> Decimal:
    """Spec §8, pinned: min_stake at the qualification bar, max_stake at
    `cap_points` above it. Bounded, monotone, no Kelly."""
    size = cfg["whale"]["size"]
    lo, hi = d(size["min_stake_usd"]), d(size["max_stake_usd"])
    x = (d(S) - d(cfg["whale"]["min_score_lb"])) / d(size["cap_points"])
    return q6(lo + min(max(x, Decimal(0)), Decimal(1)) * (hi - lo))
