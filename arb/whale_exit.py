"""L14 exits (spec §9). Split out of arb/whale_signal.py on size alone — same
contract: pure, `now` is an argument, scores arrive through `score_lookup`.

One live policy, three rules in order, pregame only. Post-start a bet is held
to resolution: in-play prices are game state, not information about the whales.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arb.models import q6
from arb.whale_merge import d, merge_windows, ts
from arb.whale_signal import holders_share, point_of, wmean


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    detail: dict


def exit_reason(bet_ctx, open_positions_now, score_lookup, now, cfg,
                ranked=frozenset(), holdings=None) -> ExitDecision | None:
    """`bet_ctx` is what the lane stored at booking: variant, side,
    condition_id, start_utc and the contributing `whales` (wallet, lb,
    open_size — a `Signal.whales` list is exactly this shape).

    `ranked` is the cycle's leaderboard set, handed in by the lane. Every
    recomputation below runs under the BET'S OWN variant, so a V4 bet is
    re-read over ranked whales only — unranked opposition was never V4
    consensus and must not flip a V4 position (§7 amendment 2026-09-04).

    `holdings` is `{wallet: contracts held now}` from the venue's own
    `positions?user=` (arb/whale_holdings.for_bet), for the second `left`
    test. A wallet absent from it has no fresh reading and keeps its
    prints-based size; None or empty disables that test entirely. This module
    stays pure — the tape read and the staleness bound live in the caller."""
    ex, variant = cfg["whale"]["exit"], bet_ctx["variant"]
    side, cid = int(bet_ctx["side"]), bet_ctx["condition_id"]
    start = ts(bet_ctx.get("start_utc"))
    if start is None or ts(now) >= start:
        return None

    # flip: the consensus itself moved, recomputed under this bet's variant
    # over the whales still HOLDING — every wallet's merged net position on
    # this market, whatever the age of the print that opened it (L14 amendment
    # 2026-09-04, `fresh_only=False`). Recounting the FRESH prints instead made
    # 12 of day one's 14 flips fire at points_side 0 / points_total 1-2 after
    # 55-153 minutes: our own whales' prints had aged out of
    # signal_window_secs while one opposing fresh print remained, and nobody
    # had sold anything. Ageing out is not a change of mind. `total == 0` —
    # nobody holds this market at all — is still no reading, and falls through
    # to `left`/`confidence`, which measure the signal's own whales directly.
    # ONE helper with §7's entry gate 1b (L14 amendment 2026-09-04): entry and
    # exit read the same holders tally, so the two can never drift apart again.
    ratio, ours, total = holders_share(variant, cid, open_positions_now,
                                       score_lookup, side, now, cfg, ranked)
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
    merged_now = merge_windows(cid, open_positions_now, d(cfg["whale"]["min_print_usd"]), now, cfg)
    open_now = {w: Decimal(0) for w in booked}
    notional_now = {}
    for (wallet, s), m in merged_now.items():
        if s == side and wallet in booked:
            open_now[wallet] = m["size"]
            notional_now[wallet] = m["notional"]
    held_then = sum(booked.values(), Decimal(0))
    held_now = sum(open_now.values(), Decimal(0))
    sold_out = (1 - d(ex["sold_fraction"])) * held_then
    if held_now <= sold_out:
        return ExitDecision("left", {"open_now": q6(held_now),
                                     "open_at_booking": q6(held_then),
                                     "via": "prints"})

    # left, second reading (L14 amendment 2026-09-04, item C): the same test
    # against what the VENUE says each whale holds. The prints can only see a
    # whale leave through a trade we collected; `positions?user=` is the
    # wallet's current book. A wallet with no fresh snapshot keeps its
    # prints-based size, so a stale reading never exits a position — it just
    # leaves this test as weak as the prints-only one it extends.
    if holdings:
        held_venue = sum((d(holdings.get(w, open_now.get(w, Decimal(0))))
                          for w in booked), Decimal(0))
        if held_venue <= sold_out:
            return ExitDecision("left", {"open_now": q6(held_venue),
                                         "open_at_booking": q6(held_then),
                                         "via": "positions"})

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
               "open_notional": q6(notional_now.get(wallet, Decimal(0))),
               "drop": q6(d(x["lb"]) - score.lb)}
        cur["points"] = point_of(variant, cur, cfg, ranked)
        remaining.append(cur)
    if not remaining:
        return None
    # V4 fix (V4 review of 3a455fd): a V4 whale's points are 1/0 rank
    # membership, not a score — once every remaining whale has fallen off
    # the leaderboard, `wmean`'s zero-weight fallback would otherwise average
    # their raw `lb` back in unweighted, exactly the leak V4's rank filter
    # exists to close. No ranked whale left to weigh is no verdict, not a
    # mean computed over the unranked crowd.
    if variant == "V4" and all(x["points"] <= 0 for x in remaining):
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
