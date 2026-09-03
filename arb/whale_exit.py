"""L14 exits (spec §9). Split out of arb/whale_signal.py on size alone — same
contract: pure, `now` is an argument, scores arrive through `score_lookup`.

One live policy, three rules in order, pregame only. Post-start a bet is held
to resolution: in-play prices are game state, not information about the whales.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb.models import q6
from arb.whale_signal import NO, YES, d, merge_windows, point_of, qualifying, tally, ts, wmean


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
    start = ts(bet_ctx.get("start_utc"))
    if start is None or ts(now) >= start:
        return None

    # flip: the consensus itself moved, recomputed under this bet's variant.
    # `total == 0` is every contributing print having simply aged out of
    # signal_window_secs — no reading at all, not a reading that flipped, so it
    # falls through to `left`/`confidence`, which measure the whales directly.
    by_side, _ = tally(variant, qualifying(cid, open_positions_now,
                                           score_lookup, now, cfg), cfg)
    total, ours = by_side[YES] + by_side[NO], by_side[side]
    ratio = q6(ours / total) if total > 0 else None
    if ratio is not None and ratio < d(ex["flip_ratio"]):
        return ExitDecision("flip", {"ratio": ratio, "points_side": ours,
                                     "points_total": total})

    # left: the whales that formed the signal sold out from under us. Rows sum
    # per wallet: a pre-merge `whales` list can name a wallet more than once.
    # `open_now`/`avg` merge current positions the same way `qualifying` does
    # (same helper) — ALL of a wallet's open windows on this side, not just
    # the ones still inside the signal window — so `left` compares like with
    # like against `booked` (spec §7, pinned 2026-09-03).
    booked: dict[str, Decimal] = {}
    for x in bet_ctx["whales"]:
        if x.get("side", side) == side:
            booked[x["wallet"]] = booked.get(x["wallet"], Decimal(0)) + d(x["open_size"])
    merged_now = merge_windows(cid, open_positions_now, d(cfg["whale"]["min_print_usd"]))
    open_now = {w: Decimal(0) for w in booked}
    avg = {}
    for (wallet, s), m in merged_now.items():
        if s == side and wallet in booked:
            open_now[wallet] = m["size"]
            avg[wallet] = q6(m["notional"] / m["size"])
    held_then = sum(booked.values(), Decimal(0))
    held_now = sum(open_now.values(), Decimal(0))
    if held_now <= (1 - d(ex["sold_fraction"])) * held_then:
        return ExitDecision("left", {"open_now": q6(held_now),
                                     "open_at_booking": q6(held_then)})

    # confidence: the whales are still here, their scores are not. Their print
    # may have aged out of the signal window by now, so this reads the holders
    # directly rather than the qualifying set.
    remaining = []
    for x in bet_ctx["whales"]:
        wallet = x["wallet"]
        if x.get("sybil_dropped") or open_now.get(wallet, Decimal(0)) <= 0:
            continue
        score = score_lookup(wallet, *_bucket(open_positions_now, cid, wallet))
        if score is None:
            continue
        cur = {"wallet": wallet, "lb": score.lb, "score": score,
               "open_notional": q6(open_now[wallet] * avg.get(wallet, Decimal(0))),
               "drop": q6(d(x["lb"]) - score.lb)}
        cur["points"] = point_of(variant, cur, cfg)
        remaining.append(cur)
    if not remaining:
        return None
    S = wmean(remaining, "lb")
    if S < d(ex["min_score_lb"]):
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
