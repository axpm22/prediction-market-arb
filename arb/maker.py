"""Maker-fill simulator: what a RESTING order would have earned.

Taking always pays the spread, and on the one fully-measured cover the spread
was 13x the edge. The only structural escape is to earn the spread instead of
paying it, which means posting rather than crossing.

The honest difficulty is queue position. We cannot observe where in the queue
a hypothetical order would sit, so a single fill count is a claim we cannot
support — arb/bench_markout.py:l7_maker says as much about its own one-sided
number. This module brackets it:

    conservative  we are LAST in queue: filled only after every contract
                  resting ahead of us at our price has traded
    optimistic    we are FIRST in queue: the first contract at our price is ours

The truth is between. The conservative bound is the pre-registered primary
(measurement rule 12); the optimistic bound is context and never the headline.

Pure by construction: no sockets, no database, no clock. Everything arrives as
an argument so every failure mode is unit-testable.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from arb.models import q6


@dataclass
class RestingOrder:
    token: str
    price: Decimal
    queue_ahead: Decimal        # contracts resting ahead of us when we posted
    posted_at: float
    consumed: Decimal = Decimal(0)
    filled: set = field(default_factory=set)


class MakerSim:
    """One resting BUY per token. Feed it observed trade size at a price."""

    def __init__(self, fee_rate: Decimal = Decimal(0)):
        self.fee_rate = Decimal(str(fee_rate))
        self.orders: dict[str, RestingOrder] = {}

    def post(self, token, price, queue_ahead, posted_at) -> RestingOrder:
        o = RestingOrder(str(token), Decimal(str(price)),
                         Decimal(str(queue_ahead)), float(posted_at))
        self.orders[o.token] = o
        return o

    def observe(self, token, price, size, at) -> list[dict]:
        """Size traded at `price` on `token`. Returns any fills it triggers.

        A resting BUY at P is only reachable by trades at P or better (lower);
        size printed at a worse price belongs to someone else's order.
        """
        o = self.orders.get(str(token))
        if o is None:
            return []
        price, size = Decimal(str(price)), Decimal(str(size))
        if size <= 0 or price > o.price:
            return []
        o.consumed += size
        out = []
        if "optimistic" not in o.filled:
            o.filled.add("optimistic")
            out.append({"token": o.token, "price": o.price, "at": float(at),
                        "basis": "optimistic"})
        if "conservative" not in o.filled and o.consumed > o.queue_ahead:
            o.filled.add("conservative")
            out.append({"token": o.token, "price": o.price, "at": float(at),
                        "basis": "conservative"})
        return out


def markout(fill: dict, exit_bid: Decimal, fee: Decimal) -> Decimal:
    """P&L per contract for a maker BUY, marked at an EXECUTABLE exit.

    Measurement rule 1 bans mids. A maker buy is unwound by selling into the
    bid, so the bid is what we could actually have got — marking at a midpoint
    would credit us half a spread nobody offered.
    """
    return q6(Decimal(str(exit_bid)) - Decimal(str(fill["price"]))
              - Decimal(str(fee)))


def _stats(vals: list[Decimal]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    fs = [float(v) for v in vals]
    mean = statistics.fmean(fs)
    sd = statistics.stdev(fs) if len(fs) > 1 else 0.0
    half = 1.96 * sd / (len(fs) ** 0.5)
    return {"n": len(fs), "mean": q6(Decimal(mean)),
            "lo": q6(Decimal(mean - half)), "hi": q6(Decimal(mean + half))}


def bracket(fills: list[dict]) -> dict:
    """Both bounds, with the primary named. Never collapse this to one number:
    the gap between the bounds IS the queue-position uncertainty."""
    by = {"conservative": [], "optimistic": []}
    for f in fills:
        if f.get("basis") in by and f.get("markout") is not None:
            by[f["basis"]].append(Decimal(str(f["markout"])))
    return {"conservative": _stats(by["conservative"]),
            "optimistic": _stats(by["optimistic"]),
            "primary": "conservative"}
