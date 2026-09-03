"""L14 window merge and qualification (§7). Split out of arb/whale_signal.py
on size alone (Task 6 fix 3) — same contract: pure, `now` is an argument.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from arb.models import q6


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


def merge_windows(condition_id, positions, min_usd, now, cfg) -> dict[tuple, dict]:
    """A wallet's several open BUY windows on the same `(market, side)` merge
    into one contributor: summed open size, notional-weighted average price,
    and the newest window (for `last_ts`/`sport`/`market_type`). Base
    eligibility only — an open pregame position on this market clearing
    `min_print_usd` — covering ALL of the wallet's open windows, however old
    (spec §7, pinned 2026-09-03): no signal-window gate here, so `qualifying`
    and `whale_exit`'s `left` test can each apply that gate on their own terms
    against the same merged exposure.

    Windows whose `last_ts` is still inside `signal_window_secs` of `now` also
    accumulate `fresh_notional`/`fresh_size`, exposed as `fresh_avg_price`:
    the `stale` gate VWAPs the fresh prints only, not the whole exposure
    (spec §7 item 4, pinned 2026-09-03)."""
    window, now_ts = int(cfg["whale"]["signal_window_secs"]), ts(now)
    merged: dict[tuple, dict] = {}
    for p in positions:
        open_size = p.size - p.sold_size
        if (p.condition_id != condition_id or not p.pregame or open_size <= 0
                or p.notional_usd < min_usd):
            continue
        m = merged.setdefault((p.wallet, p.outcome_index),
                              {"size": Decimal(0), "notional": Decimal(0), "newest": p,
                               "fresh_size": Decimal(0), "fresh_notional": Decimal(0)})
        m["size"] += open_size
        m["notional"] += open_size * p.avg_price
        if p.last_ts > m["newest"].last_ts:
            m["newest"] = p
        if p.last_ts >= now_ts - window:
            m["fresh_size"] += open_size
            m["fresh_notional"] += open_size * p.avg_price
    for m in merged.values():
        m["fresh_avg_price"] = (q6(m["fresh_notional"] / m["fresh_size"])
                                if m["fresh_size"] > 0 else Decimal(0))
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
    for m in merge_windows(condition_id, open_positions, min_usd, now, cfg).values():
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
                    "fresh_avg_price": m["fresh_avg_price"],
                    "open_notional": q6(m["notional"]), "sybil_dropped": False})
    return out
