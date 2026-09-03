"""L14 exits (spec §9). Split out of arb/whale_signal.py on size alone — same
contract: pure, `now` is an argument, scores arrive through `score_lookup`.

One live policy, three rules in order, pregame only. Post-start a bet is held
to resolution: in-play prices are game state, not information about the whales.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb.models import q6
from arb.whale_signal import NO, YES, _d, _sybil, _ts, _wmean, point_of, qualifying


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    detail: dict


def exit_reason(bet_ctx, open_positions_now, score_lookup, now, cfg) -> ExitDecision | None:
    """`bet_ctx` is what the lane stored at booking: variant, side,
    condition_id, start_utc and the contributing `whales` (wallet, lb,
    open_size — a `Signal.whales` list is exactly this shape)."""
    ex, variant = cfg["whale"]["exit"], bet_ctx["variant"]
    side, cid = int(bet_ctx["side"]), bet_ctx["condition_id"]
    start = _ts(bet_ctx.get("start_utc"))
    if start is None or _ts(now) >= start:
        return None

    # flip: the consensus itself moved, recomputed under this bet's variant.
    live = qualifying(cid, open_positions_now, score_lookup, now, cfg)
    for s in (YES, NO):
        _sybil([x for x in live if x["side"] == s],
               int(cfg["whale"]["fire"]["sybil_gap_secs"]))
    for x in live:
        x["points"] = Decimal(0) if x["sybil_dropped"] else point_of(variant, x, cfg)
    total = sum((x["points"] for x in live), Decimal(0))
    ours = sum((x["points"] for x in live if x["side"] == side), Decimal(0))
    ratio = q6(ours / total) if total > 0 else Decimal(0)
    if ratio < _d(ex["flip_ratio"]):
        return ExitDecision("flip", {"ratio": ratio, "points_side": q6(ours),
                                     "points_total": q6(total)})

    # left: the whales that formed the signal sold out from under us.
    booked = {x["wallet"]: _d(x["open_size"]) for x in bet_ctx["whales"]
              if x.get("side", side) == side}
    open_now = {w: Decimal(0) for w in booked}
    avg = {}
    for p in open_positions_now:
        if p.condition_id == cid and p.outcome_index == side and p.wallet in booked:
            open_now[p.wallet] += p.size - p.sold_size
            avg.setdefault(p.wallet, p.avg_price)
    held_then = sum(booked.values(), Decimal(0))
    held_now = sum(open_now.values(), Decimal(0))
    if held_now <= (1 - _d(ex["sold_fraction"])) * held_then:
        return ExitDecision("left", {"open_now": q6(held_now),
                                     "open_at_booking": q6(held_then)})

    # confidence: the whales are still here, their scores are not. Their print
    # may have aged out of the signal window by now, so this reads the holders
    # directly rather than the qualifying set.
    remaining = []
    for x in bet_ctx["whales"]:
        wallet = x["wallet"]
        if open_now.get(wallet, Decimal(0)) <= 0:
            continue
        score = score_lookup(wallet, *_bucket(open_positions_now, cid, wallet))
        if score is None:
            continue
        cur = {"wallet": wallet, "lb": score.lb, "score": score,
               "open_notional": q6(open_now[wallet] * avg.get(wallet, Decimal(0))),
               "drop": q6(_d(x["lb"]) - score.lb)}
        cur["points"] = point_of(variant, cur, cfg)
        remaining.append(cur)
    if not remaining:
        return None
    S = _wmean(remaining, "lb")
    if S < _d(ex["min_score_lb"]):
        # Pooling means a loss in another sport can lower a score here: §9 wants
        # that auditable, so name the whale that moved and the n at each level.
        worst = max(remaining, key=lambda x: x["drop"])
        return ExitDecision("confidence", {
            "S": S, "wallet": worst["wallet"], "drop": worst["drop"],
            "lb": worst["lb"], "n_bucket": worst["score"].n_bucket,
            "n_sport": worst["score"].n_sport, "n_root": worst["score"].n_root})
    return None


def _bucket(positions, cid, wallet) -> tuple:
    """The (sport, market_type) this wallet is scored in on this market."""
    for p in positions:
        if p.condition_id == cid and p.wallet == wallet:
            return p.sport, p.market_type
    return None, None
