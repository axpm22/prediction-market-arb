"""L14 variant V6 "dominance" (design §7, added 2026-09-04): how much whale
MONEY sits on a side, not how many heads.

Counting whales cannot see money, and the tape says that matters: 12 of the 56
fired V1 signals to 2026-09-04 (21%) had the whales' net dollars on the side
the count-based rule did NOT buy. One whale's points on its own side are

    q_w . N_w . R_w . F_w / max(V_24h, dominance.min_volume_usd)

    q_w = max(score_lb - min_score_lb, 0)   the V2 excess, clipped at zero
    N_w = open_notional                     live exposure, SELLs already netted
    R_w = 0.5 ** (age / half_life_secs)     recency of its newest print
    F_w = clip(1 - max(ask - price, 0) / fire.max_drift_points, 0, 1)
                                            price-relative freshness: a whale
                                            that bought far below the current
                                            touch bought a move that already
                                            happened

Every point is non-negative, so the ratio gate and the `min_points` comparison
work unchanged — V6 needs no new gate and no branch in `_signal`.

Split out of arb/whale_signal.py on size alone (that module is at its ~300
line cap); same contract: PURE, no clock — `now_ts` arrives in the context the
caller builds — and no venue call.

Rule 4: every threshold from `cfg["whale"]`. Rule 5: Decimal everywhere, with
        one float exponent for the half-life weight, re-quantized on its own
        line exactly as V3's `math.log10` weight already is.
"""
from __future__ import annotations

from decimal import Decimal

from arb.models import q6
from arb.whale_merge import d

# Each key of the context is optional and a missing one means NO INFORMATION,
# never zero: `volume` unknown reads as the floor (an unknown market must not
# read as "tiny market => unbounded dominance"), `now_ts` unknown leaves the
# recency weight at 1, and a side with no `ask` leaves the freshness weight at
# 1. That is what lets the read-only dashboard and any caller without a book
# evaluate a V6 bet at all — and it never suppresses opposition, which is the
# conservative direction for an exit recount.
CTX_KEYS = ("volume", "now_ts", "ask")


def point(whale, cfg, ctx=None) -> Decimal:
    """V6's points for ONE whale on its own side (design §7)."""
    w, dom = cfg["whale"], cfg["whale"]["dominance"]
    ctx = ctx or {}
    excess = max(d(whale["lb"]) - d(w["min_score_lb"]), Decimal(0))
    notional = d(whale.get("open_notional") or 0)
    volume, floor = ctx.get("volume"), d(dom["min_volume_usd"])
    denom = max(d(volume), floor) if volume is not None else floor
    return q6(excess * notional * _recency(whale, ctx, dom)
              * _freshness(whale, ctx, w) / denom)


def _recency(whale, ctx, dom) -> Decimal:
    """`0.5 ** (age / half_life_secs)` on the whale's newest print. A print at
    the edge of `signal_window_secs` (7,200) carries 0.06 of one made now."""
    now_ts, last_ts = ctx.get("now_ts"), whale.get("last_ts")
    if now_ts is None or last_ts is None:
        return Decimal(1)
    age = max(int(now_ts) - int(last_ts), 0)     # a clock skew is not a boost
    return q6(Decimal(str(0.5 ** (age / float(dom["half_life_secs"])))))


def _freshness(whale, ctx, w) -> Decimal:
    """Linear in the ask's drift above the whale's own entry price, zero at
    `fire.max_drift_points`.

    The registered formula names `fresh_avg_price`, which is what every whale
    in an ENTRY tally has. A whale whose prints have aged out of
    `signal_window_secs` has none — it appears only in the holders recount
    (§7 item 1b) and in exits — and falls back to `avg_price`, its whole open
    exposure's VWAP. Reading a missing fresh VWAP as 0 instead would make the
    drift the full ask, zero every stale holder's points, and quietly delete
    the standing opposition gate 1b exists to measure; the fallback is the
    reading that books FEWER signals.
    """
    ask = (ctx.get("ask") or {}).get(whale.get("side"))
    price = d(whale.get("fresh_avg_price") or 0)
    if price <= 0:
        price = d(whale.get("avg_price") or 0)
    if ask is None or price <= 0:
        return Decimal(1)
    drift = max(d(ask) - price, Decimal(0))
    return max(q6(1 - drift / d(w["fire"]["max_drift_points"])), Decimal(0))


def market_volume(volumes, tokens, condition_id):
    """The market's own 24h volume for the denominator, off the watch quotes
    the zombie guard already reads (`arb/lanes/whale.py::VOLUME_SQL`). Those
    rows are per TOKEN and both tokens of one Polymarket market carry the same
    market-level `volume24hr`, so either side answers; unknown stays None,
    which `point` reads as the floor, never as zero."""
    for side in (0, 1):
        volume = volumes.get(f"{condition_id}:{tokens.get((condition_id, side))}")
        if volume is not None:
            return volume
    return None
