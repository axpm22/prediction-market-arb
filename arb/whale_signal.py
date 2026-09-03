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
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median

from arb.models import q6

VARIANTS = ("V1", "V2", "V3")
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


def ts(v) -> int | None:
    """Epoch seconds from an int, a datetime or an ISO string (naive = UTC)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    return int((v if v.tzinfo else v.replace(tzinfo=timezone.utc)).timestamp())


def d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


# -------------------------------------------------------------------- points

def point_of(variant, whale, cfg) -> Decimal:
    """Spec §7's variant table for one whale dict (`lb`, `open_notional`)."""
    if variant == "V1":
        return Decimal(1)
    excess = d(whale["lb"]) - d(cfg["whale"]["min_score_lb"])
    if variant == "V2":
        return q6(excess)
    unit = d(cfg["whale"]["size"]["unit_usd"])
    weight = d(math.log10(1 + float(d(whale["open_notional"]) / unit)))
    return q6(excess * weight)


def points(variant, whales_on_side, cfg) -> Decimal:
    """One side's total. Sybil-dropped whales contribute nothing, so this
    agrees with `Signal.points_yes/points_no` — but the caller must hand in ONE
    side: `Signal.whales` carries both, each with its own `side`."""
    return q6(sum((point_of(variant, w, cfg) for w in whales_on_side
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

def merge_windows(condition_id, positions, min_usd) -> dict[tuple, dict]:
    """A wallet's several open BUY windows on the same `(market, side)` merge
    into one contributor: summed open size, notional-weighted average price,
    and the newest window (for `last_ts`/`sport`/`market_type`). Base
    eligibility only — an open pregame position on this market clearing
    `min_print_usd` — covering ALL of the wallet's open windows, however old
    (spec §7, pinned 2026-09-03): no signal-window gate here, so `qualifying`
    and `whale_exit`'s `left` test can each apply that gate on their own terms
    against the same merged exposure."""
    merged: dict[tuple, dict] = {}
    for p in positions:
        open_size = p.size - p.sold_size
        if (p.condition_id != condition_id or not p.pregame or open_size <= 0
                or p.notional_usd < min_usd):
            continue
        m = merged.setdefault((p.wallet, p.outcome_index),
                              {"size": Decimal(0), "notional": Decimal(0), "newest": p})
        m["size"] += open_size
        m["notional"] += open_size * p.avg_price
        if p.last_ts > m["newest"].last_ts:
            m["newest"] = p
    return merged


def qualifying(condition_id, open_positions, score_lookup, now, cfg) -> list[dict]:
    """Spec §7's qualifying whales, ONE ROW PER WALLET PER SIDE (pinned
    2026-09-03): a wallet's several BUY windows on the same (market, side) are
    one contributor — summed open size, notional-weighted average price, newest
    `last_ts` — scored once and voting once.

    `merge_windows` merges ALL of a wallet's open windows on that side (its
    live exposure) regardless of age; `signal_window_secs` then gates
    QUALIFICATION only, through the merged wallet's newest window's `last_ts`
    (spec §7, pinned 2026-09-03). The merged wallet then needs a score
    clearing `min_score_lb`; sport and market type come from its newest
    window, which is what settlement and scoring already agreed on.
    """
    w = cfg["whale"]
    window, min_lb = int(w["signal_window_secs"]), d(w["min_score_lb"])
    min_usd, now_ts = d(w["min_print_usd"]), ts(now)
    out = []
    for m in merge_windows(condition_id, open_positions, min_usd).values():
        p = m["newest"]
        if now_ts - p.last_ts > window:
            continue
        score = score_lookup(p.wallet, p.sport, p.market_type)
        if score is None or score.lb < min_lb:
            continue
        out.append({"wallet": p.wallet, "side": p.outcome_index, "lb": score.lb,
                    "n_bucket": score.n_bucket, "native": score.native,
                    "open_size": q6(m["size"]), "last_ts": p.last_ts,
                    "avg_price": q6(m["notional"] / m["size"]),
                    "open_notional": q6(m["notional"]), "sybil_dropped": False})
    return out


def sybil(whales, gap) -> None:
    """Spec §7.3: same-side prints within `gap` seconds are one whale.

    Single linkage on `last_ts`: sort by time, cut wherever the step from the
    previous print exceeds `gap`. A chain A-B-C, each within `gap` of its
    neighbour but not of the far end, is therefore ONE whale, and the answer
    depends only on the timestamps — the earlier greedy pass against a kept set
    left 1 or 2 survivors for the same three prints depending on which held the
    top `lb`. Highest `lb` in a cluster survives (ties to the lowest wallet);
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


def tally(variant, whales, cfg) -> tuple[dict, dict]:
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
                                     else point_of(variant, x, cfg))) for x in side]
    return ({s: q6(sum((x["points"] for x in ws), Decimal(0)))
             for s, ws in scored.items()}, scored)


# ------------------------------------------------------------------ evaluate

def evaluate(market, open_positions, score_lookup, touch, now, cfg, m0) -> list[Signal]:
    """Three pre-registered variants per market (§7). Empty when no whale
    qualifies on either side — nothing to persist, nothing left on the table."""
    whales = qualifying(market["condition_id"], open_positions, score_lookup, now, cfg)
    if not whales:
        return []
    return [_signal(v, market, whales, touch, now, cfg, m0) for v in VARIANTS]


def _signal(variant, market, whales, touch, now, cfg, m0) -> Signal:
    w, f = cfg["whale"], cfg["whale"]["fire"]
    total, scored = tally(variant, whales, cfg)
    side = YES if total[YES] >= total[NO] else NO      # a tie fails `ratio` below
    denom = total[YES] + total[NO]
    ratio = q6(total[side] / denom) if denom > 0 else Decimal(0)

    live = [x for x in scored[side] if not x["sybil_dropped"]]
    vwap = wmean(live, "avg_price") if live else Decimal(0)
    S = wmean(live, "lb") if live else Decimal(0)
    t = touch.get(side)
    ask, bid = (t.ask, t.bid) if t else (None, None)
    depth = t.depth_usd if t else Decimal(0)
    threshold = f["min_points"][variant]
    age = ts(now) - max((x["last_ts"] for x in live), default=ts(now))
    to_start = (ts(market.get("start_utc")) or 0) - ts(now)

    if ratio < d(f["ratio"]):
        reason = "ratio"
    elif threshold is not None and total[side] < d(threshold):
        reason = "min-points"
    elif threshold is None:                            # §7.2 calibration window
        reason = "uncalibrated"
    elif age >= int(w["max_print_age_secs"]):          # §13: strictly younger
        reason = "late-print"
    elif ask is not None and ask > vwap + d(f["max_drift_points"]):
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
                  suppressed_reason=reason, flagged_edge=q6(S - d(m0)), S=S)


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
