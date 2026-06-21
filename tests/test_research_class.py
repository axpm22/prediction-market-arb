from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.arbitrage import (
    capturable_lower_bound,
    detect_true_arbs,
    has_unverifiable_leg,
)
from arb.models import Quote
from arbnext.analysis.leadlag import _moves

EVENT = "baseball_mlb:2026-06-12:kansas city royals:texas rangers:3"


def q(venue: str, outcome: str, cost: str, depth: str = "500") -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=EVENT, venue=venue, book=f"{venue}-{outcome}", market_type="WINNER",
        line=None, outcome_label=outcome, raw_price=price, cost=price, fee_cost=Decimal("0"),
        depth_usd=Decimal(depth), live=False, fetched_at=datetime.now(timezone.utc),
        start_utc=datetime.now(timezone.utc) + timedelta(hours=4),
    )


def detect(quotes):
    return detect_true_arbs(quotes, bankroll=Decimal("1000"), max_stake_pct=Decimal("0.10"),
                            trusted_pairs=set(), stale_warn=Decimal("0.04"))


def test_sx_is_now_verifiable_not_research() -> None:
    # SX legs carry an outcome-side ref, so they are verifiable now (no longer RESEARCH).
    assert not has_unverifiable_leg([q("kalshi", "a", "0.48"), q("sx", "b", "0.48")])
    assert not has_unverifiable_leg([q("kalshi", "a", "0.48"), q("polymarket", "b", "0.48")])
    [opp] = detect([q("kalshi", "kansas city royals", "0.48"), q("sx", "texas rangers", "0.48")])
    assert "research-unverifiable" not in opp.notes


def test_verifiable_arb_is_not_research() -> None:
    [opp] = detect([q("kalshi", "kansas city royals", "0.48"), q("polymarket", "texas rangers", "0.48")])
    assert "research-unverifiable" not in opp.notes


def test_capturable_lower_bound_is_min_depth_times_roi() -> None:
    legs = [q("kalshi", "a", "0.48", depth="300"), q("polymarket", "b", "0.48", depth="120")]
    roi = Decimal("0.041667")
    # floor = min(300,120) * roi
    assert capturable_lower_bound(legs, roi) == (Decimal("120") * roi).quantize(Decimal("0.000001"))
    # and it is reported on the flag
    [opp] = detect(legs)
    assert any(n.startswith("capturable:") for n in opp.notes)


def test_leadlag_move_detection() -> None:
    base = datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc)
    series = [(base, 0.50), (base + timedelta(seconds=30), 0.505),  # below threshold
              (base + timedelta(seconds=60), 0.52),                 # +0.015 move up
              (base + timedelta(seconds=90), 0.50)]                 # -0.02 move down
    moves = _moves(series, threshold=0.01)
    assert [d for _, d in moves] == [1, -1]
