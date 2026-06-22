from datetime import datetime, timezone
from decimal import Decimal

from arb.arbitrage import detect_true_arbs, equalized_arb_stakes
from arb.models import Event, Opportunity, Quote
from arb.quality import classify_quality
from arb.settlement import _leg_result


def leg(outcome: str, cost: str, venue: str = "kalshi", depth: str = "500") -> Quote:
    c = Decimal(cost)  # 2-way sport so detection isn't blocked by the soccer 3-way guard
    return Quote(event_key="baseball_mlb:2026-06-22:france:iraq", venue=venue, book=f"{venue}-{outcome}",
                 market_type="WINNER", line=None, outcome_label=outcome, raw_price=c, cost=c,
                 fee_cost=Decimal("0"), depth_usd=Decimal(depth), live=False,
                 fetched_at=datetime.now(timezone.utc),
                 start_utc=datetime.now(timezone.utc))


# ---- #8 equalized stakes + guaranteed floor ----

def test_equalized_stakes_report_real_floor() -> None:
    legs = [leg("france", "0.48"), leg("iraq", "0.49")]
    stakes, floor = equalized_arb_stakes(legs, budget=Decimal("100"), total_cost=Decimal("0.97"))
    assert floor > 0   # a healthy arb keeps a positive worst-case profit after rounding
    # floor is the min per-outcome payout minus total staked, not the idealized ROI*budget.


def test_rounding_broke_lock_flagged_on_tiny_budget() -> None:
    # $1 budget across two ~0.5 legs: cent rounding can't equalize payouts -> floor may go
    # negative; the arb must be flagged, not reported as locked profit.
    legs = [leg("france", "0.50"), leg("iraq", "0.49")]
    opps = detect_true_arbs(legs, bankroll=Decimal("1"), max_stake_pct=Decimal("1.0"),
                            trusted_pairs=set(), stale_warn=Decimal("0.04"))
    assert opps
    o = opps[0]
    assert any(n.startswith("floor:") for n in o.notes)
    if o.edge > 0:  # if rounding broke the lock the floor note is negative and it's REVIEW
        floor_note = next(n for n in o.notes if n.startswith("floor:"))
        floor = Decimal(floor_note.split(":")[1])
        if floor < 0:
            assert "rounding-broke-lock" in o.notes and o.status == "REVIEW"


# ---- #9 suspect-edge uses comparable probability-points, not the kind-specific ratio ----

def _ev_opp(edge_ratio: str, edge_pts: str) -> Opportunity:
    ev = Event(event_key="e", category="sports", sport="baseball_mlb", home="a", away="b",
               start_utc=datetime(2026, 6, 22, tzinfo=timezone.utc))
    q = leg("a", "0.05")  # cheap contract
    return Opportunity(kind="EV", category="sports", event=ev, legs=[q],
                       edge=Decimal(edge_ratio), edge_points=Decimal(edge_pts), stakes={}, status="REVIEW")


def test_cheap_ev_not_falsely_quarantined() -> None:
    # Cheap contract: ratio (p-c)/c is huge (would trip 0.25), but p-c is only 5 points.
    opp = _ev_opp(edge_ratio="1.00", edge_pts="0.05")
    assert classify_quality(opp, Decimal("50"), Decimal("0.25")) is None  # not suspect


def test_real_inflated_edge_still_quarantined() -> None:
    # Genuine stale-book leak: huge probability-points gap.
    opp = _ev_opp(edge_ratio="49.0", edge_pts="0.49")
    assert classify_quality(opp, Decimal("50"), Decimal("0.25")) == "suspect-edge"


# ---- #10 DRAW settlement ----

def test_draw_leg_wins_on_tie() -> None:
    scores = {"france": Decimal("1"), "iraq": Decimal("1")}  # 1-1 draw
    assert _leg_result({"outcome_label": "draw", "market_type": "WINNER"}, scores) == "WON"
    assert _leg_result({"outcome_label": "france", "market_type": "WINNER"}, scores) == "LOST"


def test_team_wins_when_it_wins() -> None:
    scores = {"france": Decimal("2"), "iraq": Decimal("1")}
    assert _leg_result({"outcome_label": "france", "market_type": "WINNER"}, scores) == "WON"
    assert _leg_result({"outcome_label": "draw", "market_type": "WINNER"}, scores) == "LOST"
