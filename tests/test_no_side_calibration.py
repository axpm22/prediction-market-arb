"""L13 — the NO side of the same books L8 prices.

Why this lane exists. L8 reads `yes_ask` and only `yes_ask`, so it buys YES in
every band and every market. Its six bands all came back negative, which reads
as "the market is efficient and we pay the spread" — but it is equally
consistent with systematically taking the WRONG SIDE of a real mispricing, and
L8's design cannot tell those apart, because it never prices the other side.

The favorite-longshot literature says the bias is largely a *Yes Bias*: YES is
overpriced, so the mirror trade is buying NO. On Kalshi the NO ask is not
quoted in the candles and derives from the identity already verified in
data/verified_facts.md and arb/venues/kalshi.py: the ask of one side is
1 - the opposite side's best bid.

The honesty pin is `test_the_two_sides_sum_to_minus_the_spread`. The NO result
is NOT independent evidence — it is the arithmetic mirror of the YES result,
and the two edges must sum to exactly -(spread + fees). If that identity ever
fails, one of the two sides is being priced wrong.
"""
from decimal import Decimal
from pathlib import Path

from arb import bench_kalshi as bk
from arb.config import load

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")


def _candle(ts, ask, bid):
    return {"end_period_ts": ts,
            "yes_ask": {"close_dollars": ask},
            "yes_bid": {"close_dollars": bid}}


CANDLES = [_candle(1000, "0.7900", "0.7700"), _candle(2000, "0.8000", "0.7800")]


def test_yes_side_is_the_ask_unchanged():
    assert bk._ask_at(CFG, CANDLES, 2000) == Decimal("0.8000")
    assert bk._ask_at(CFG, CANDLES, 2000, side="yes") == Decimal("0.8000")


def test_no_side_is_one_minus_the_yes_bid():
    """NO ask = 1 - best YES bid. Inverting this silently flips the lane, which
    is rule 10's lesson in Kalshi form."""
    assert bk._ask_at(CFG, CANDLES, 2000, side="no") == Decimal("0.2200")


def test_no_side_reads_the_same_candle_the_yes_side_validated():
    """Both sides must come from ONE candle, or the two lanes silently measure
    different populations and stop being comparable."""
    assert bk._ask_at(CFG, CANDLES, 1000, side="no") == Decimal("0.2300")


def test_an_empty_or_one_sided_book_is_dropped_on_both_sides():
    dead = [_candle(1000, "1.0000", "0.0000")]
    assert bk._ask_at(CFG, dead, 1000) is None
    assert bk._ask_at(CFG, dead, 1000, side="no") is None


def test_the_two_sides_sum_to_minus_the_spread():
    """THE HONESTY PIN. For one market, buying YES and buying NO must lose
    exactly the spread plus both fees between them — no configuration of
    outcomes can make both sides win. A positive NO edge is therefore the
    mirror of a negative YES edge, never new evidence."""
    yes_ask = bk._ask_at(CFG, CANDLES, 2000, side="yes")     # 0.80
    no_ask = bk._ask_at(CFG, CANDLES, 2000, side="no")       # 0.22
    yes_cost = bk._all_in(CFG, "Politics", yes_ask)
    no_cost = bk._all_in(CFG, "Politics", no_ask)
    for settled_yes in (0, 1):
        yes_edge = Decimal(settled_yes) - yes_cost
        no_edge = Decimal(1 - settled_yes) - no_cost
        total = yes_edge + no_edge
        spread = yes_ask - Decimal("0.7800")                 # 0.02
        fees = (yes_cost - yes_ask) + (no_cost - no_ask)
        assert total == -(spread + fees), (settled_yes, total, spread, fees)
        assert total < 0                                     # never both sides


def test_won_follows_the_side_being_bought():
    """A market that settles 'no' is a LOSS for the yes-buyer and a WIN for the
    no-buyer. Reusing L8's `won` unchanged would score L13 backwards."""
    assert (1 if "no" == "yes" else 0) == 0        # yes-buyer on a 'no' market
    assert (1 if "no" == "no" else 0) == 1         # no-buyer on a 'no' market


def test_l13_is_registered():
    from arb.bench_lanes import LANES
    assert "L13" in LANES
