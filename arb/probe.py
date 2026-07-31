"""Strategy probes: hypotheses that only became testable once the Polymarket
market WebSocket was verified keyless (data/verified_facts.md, 2026-07-30).

At our 60s full-surface cadence we observe roughly ONE book state in 156, so
every "we never saw X" conclusion so far is an upper bound on X, never a
measurement of it. These probes look where that blindness actually mattered.

  S-A  transient same-venue coherence on Polymarket.
       Kalshi cannot produce a same-venue ARB at all — NO_ask is DERIVED as
       1 - YES_bid, so YES_ask + NO_ask = 1 + spread by construction, in
       400,000 of 400,000 observations. Polymarket is different: YES and NO
       are independent CLOB tokens held together only by arbitrageurs and the
       $1 merge/split. At 60s we saw the identity hold 99.94%, but that is 1
       state in 156. If the two books ever cross intraday, it is visible only
       here, and it is a SINGLE-VENUE edge — no settlement divergence, no
       inter-leg skew, the two failure modes that killed everything else.

  S-B  achievable inter-leg skew on a focused pair.
       The 60s cadence is a full-surface constraint, not a per-market one.
       Narrowed to a few tickers, Kalshi tolerates ~1-2s polling, which is
       inside execution.max_actionable_skew_secs. Measures the skew actually
       achieved, so the cross-venue verdict rests on a real number.

The state machine is pure and unit-tested; the live runners only feed it.
Read-only, no orders, no credits (rule 1).
"""
from __future__ import annotations

from decimal import Decimal


def _best(levels, side: str):
    """(price, size) at the touch. asks -> lowest, bids -> highest."""
    best = None
    for lv in levels or []:
        try:
            p, s = Decimal(str(lv["price"])), Decimal(str(lv["size"]))
        except (KeyError, TypeError, ArithmeticError):
            continue
        if p <= 0 or p >= 1 or s <= 0:
            continue          # sentinel/empty-book prices are not quotes
        if best is None or (p < best[0] if side == "asks" else p > best[0]):
            best = (p, s)
    return best


class CoherenceState:
    """Live YES/NO book state for ONE Polymarket condition, fed by WS frames.

    Holds the last known touch per token and reports the cover sum whenever
    both sides are known. A sum below 1 means buying YES and NO together costs
    less than the $1 they are guaranteed to pay — a same-venue cover.
    """

    def __init__(self, yes_token: str, no_token: str, fee_rate=Decimal(0)):
        self.yes, self.no = str(yes_token), str(no_token)
        self.fee_rate = Decimal(str(fee_rate))
        self.books: dict[str, dict] = {}
        self.updates = 0
        self.best_sum: Decimal | None = None
        self.hits: list[dict] = []

    def apply(self, frame: dict) -> dict | None:
        """Feed one WS frame. Returns a hit dict when the touch sum < 1."""
        aid = str(frame.get("asset_id") or "")
        if aid not in (self.yes, self.no):
            return None
        ask = _best(frame.get("asks"), "asks")
        bid = _best(frame.get("bids"), "bids")
        if ask is None:
            return None
        prev = self.books.get(aid, {})
        self.books[aid] = {"ask": ask, "bid": bid or prev.get("bid"),
                           "ts": frame.get("timestamp")}
        self.updates += 1
        y, n = self.books.get(self.yes), self.books.get(self.no)
        if not y or not n:
            return None
        total = y["ask"][0] + n["ask"][0]
        # fees are charged per leg on a taker fill; ignoring them would report
        # a cover that a real fill never had
        total += (y["ask"][0] + n["ask"][0]) * self.fee_rate
        if self.best_sum is None or total < self.best_sum:
            self.best_sum = total
        if total >= 1:
            return None
        size = min(y["ask"][1], n["ask"][1])
        hit = {"sum": total, "edge": Decimal(1) - total,
               "yes_ask": y["ask"][0], "no_ask": n["ask"][0],
               "size": size, "notional": size * total,
               "ts_yes": y.get("ts"), "ts_no": n.get("ts")}
        self.hits.append(hit)
        return hit

    def apply_touch(self, asset_id: str, best_bid, best_ask) -> dict | None:
        """Feed a touch directly. `price_change` frames carry `best_bid` and
        `best_ask` per change, so the touch arrives stated — reconstructing a
        book from deltas would be strictly more code and strictly more wrong.
        """
        try:
            ask = Decimal(str(best_ask))
        except (TypeError, ArithmeticError):
            return None
        if not (0 < ask < 1):
            return None
        bid = None
        try:
            b = Decimal(str(best_bid))
            bid = b if 0 < b < 1 else None
        except (TypeError, ArithmeticError):
            bid = None
        # size is unknown from a touch quote; carry 0 so notional is never
        # overstated, and let the caller treat size as unmeasured
        return self.apply({"asset_id": asset_id,
                           "asks": [{"price": str(ask), "size": "1"}],
                           "bids": ([{"price": str(bid), "size": "1"}]
                                    if bid is not None else [])})

    def feed_ws(self, frame: dict) -> list[dict]:
        """Adapt one raw Polymarket WS frame. Returns any covers it produced.

        Verified shape 2026-07-30: `book` frames carry asset_id + bids/asks;
        `price_change` frames carry `price_changes[]`, each with its OWN
        asset_id plus best_bid/best_ask. Reading `changes[]` or a root-level
        asset_id silently drops ~99% of updates — 4 parsed of 935 observed.
        """
        et = frame.get("event_type")
        hits = []
        if et == "book":
            h = self.apply(frame)
            if h:
                hits.append(h)
        elif et == "price_change":
            for ch in frame.get("price_changes") or []:
                h = self.apply_touch(ch.get("asset_id"), ch.get("best_bid"),
                                     ch.get("best_ask"))
                if h:
                    hits.append(h)
        return hits

    def summary(self) -> dict:
        return {"updates": self.updates,
                "best_sum": self.best_sum,
                "covers": len(self.hits),
                "max_edge": max((h["edge"] for h in self.hits), default=None),
                "max_notional": max((h["notional"] for h in self.hits),
                                    default=None)}


def skew_stats(pairs: list[tuple[float, float]]) -> dict:
    """Inter-leg skew in seconds from (t_leg_a, t_leg_b) observation times.

    A cover is only actionable if both legs were observed close enough
    together that neither has moved — §8.4 bounds this at
    execution.max_actionable_skew_secs.
    """
    if not pairs:
        return {"n": 0}
    sk = sorted(abs(a - b) for a, b in pairs)
    return {"n": len(sk), "min": sk[0], "median": sk[len(sk) // 2],
            "p95": sk[min(len(sk) - 1, int(0.95 * len(sk)))], "max": sk[-1]}
