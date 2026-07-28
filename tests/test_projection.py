"""Read-time capacity projection. Fixtures only — no live DB, no HTTP."""
import json
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.projection import (BIND_BANKROLL, BIND_LADDER, BIND_TOUCH,
                            _zero_edge_contracts, project_position)

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")


def _leg(venue, cost, fee="0.001"):
    return {"venue": venue, "cost": cost, "fee_cost": fee,
            "market_type": "WINNER", "line": None, "outcome": "x", "side": "YES"}


def _q(depth, ladder=None):
    return {"depth_usd": depth, "cost": "0.5",
            "ladder_json": json.dumps(ladder) if ladder else None}


def test_three_ceilings_are_reported_separately():
    """Conflating them made ladder sizing look WORSE than touch sizing, because
    the bankroll cap was applied to one and not the other."""
    deep = [[0.49, 100000]]                       # effectively unlimited book
    p = project_position(CFG, [_leg("polymarket", "0.49"), _leg("kalshi", "0.49")],
                         [_q("50000", deep), _q("50000", deep)])
    assert p["bankroll_contracts"] < p["touch_contracts"], "bankroll should bind here"
    assert p["binder"] == BIND_BANKROLL
    # each ceiling carries its own P&L, so the caller can compare like with like
    from arb.models import q6
    for k in ("bankroll", "touch", "ladder", "zero_edge"):
        assert p[f"{k}_pnl_usd"] == q6(p[f"{k}_contracts"] * p["net_edge"])


def test_a_leg_without_a_ladder_falls_back_to_touch_and_says_so():
    """Kalshi stores no ladder for any of 3.9M quotes; sizing.arb_projection
    returns None for such a position, which silently erases every Kalshi flag.
    Here it must still project, and name the venue whose book we cannot read."""
    p = project_position(CFG, [_leg("polymarket", "0.40"), _leg("kalshi", "0.55")],
                         [_q("100", [[0.40, 200], [0.41, 5000]]), _q("100", None)])
    assert p is not None
    assert p["no_ladder_venues"] == ["kalshi"]
    assert p["ladder_contracts"] <= p["touch_contracts"] * 2  # kalshi still caps it


def test_walking_further_is_never_worse_than_stopping_at_the_touch():
    ladder = [[0.40, 10], [0.41, 50], [0.42, 500]]
    p = project_position(CFG, [_leg("polymarket", "0.40"), _leg("sx", "0.40")],
                         [_q("4", ladder), _q("4", ladder)])
    assert p["zero_edge_contracts"] >= p["ladder_contracts"] >= p["touch_contracts"]


def test_zero_edge_walk_stops_when_the_edge_is_consumed():
    """The theoretical ceiling: VWAP may reach the limit price, never exceed it."""
    ladder = [(Decimal("0.40"), Decimal(10)), (Decimal("0.60"), Decimal(1000))]
    n = _zero_edge_contracts(ladder, Decimal("0.50"))
    assert n > 10                       # takes all of level 1 and part of level 2
    filled, cost = Decimal(0), Decimal(0)
    for price, size in ladder:
        take = min(size, n - filled)
        filled += take; cost += price * take
        if filled >= n:
            break
    assert cost / filled <= Decimal("0.5000001")


def test_no_edge_projects_nothing():
    assert project_position(CFG, [_leg("kalshi", "0.60"), _leg("sx", "0.60")],
                            [_q("100"), _q("100")]) is None
