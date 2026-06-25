"""Phase 1c regression: only a whitelisted venue-pair settles into the clean (non-REVIEW) lane.

This protects the one config line that turns a cross-venue arb from REVIEW into a real zero-risk
lock. A `kalshi+polymarket:baseball_mlb` pair in `trusted_pairs` → status OK → trusted=1 → clean;
everything else (e.g. `polymarket+sx`) stays REVIEW and is excluded from headline P&L.
"""
from datetime import datetime, timezone
from decimal import Decimal

from arb.arbitrage import detect_true_arbs, status_for_legs, venue_pair_key
from arb.models import Quote

NOW = datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc)
START = datetime(2026, 6, 20, 23, 5, tzinfo=timezone.utc)
WHITELIST = {"kalshi+polymarket:baseball_mlb"}


def leg(event_key, outcome, cost, venue, *, notes=()):
    return Quote(
        event_key=event_key, venue=venue, book=f"{venue}-{outcome}", market_type="WINNER",
        line=None, outcome_label=outcome, raw_price=Decimal(cost), cost=Decimal(cost),
        fee_cost=Decimal("0"), depth_usd=Decimal("500"), live=False, fetched_at=NOW,
        start_utc=START, home="aaa", away="bbb", notes=notes,
    )


EVENT_A = "baseball_mlb:2026-06-20:aaa:bbb"   # kalshi + polymarket (whitelisted)
EVENT_B = "baseball_mlb:2026-06-20:ccc:ddd"   # polymarket + sx (not whitelisted)


def _kp():
    return [leg(EVENT_A, "aaa", "0.49", "kalshi"), leg(EVENT_A, "bbb", "0.49", "polymarket")]


def _ps():
    # SX legs always carry settlement-rule-review (their void rules differ); kept REVIEW.
    return [leg(EVENT_B, "ccc", "0.49", "polymarket"),
            leg(EVENT_B, "ddd", "0.49", "sx", notes=("settlement-rule-review",))]


def test_whitelisted_pair_passes_the_gate():
    status, _ = status_for_legs(_kp(), WHITELIST)
    assert status == "OK"


def test_unwhitelisted_pair_stays_review():
    status, notes = status_for_legs(_ps(), WHITELIST)
    assert status == "REVIEW" and "settlement-rule-review" in notes
    # and with NO whitelist, the kalshi+polymarket pair is REVIEW too — the gate is the only opener.
    assert status_for_legs(_kp(), set())[0] == "REVIEW"


def test_detection_routes_only_the_whitelisted_pair_to_ok():
    arbs = detect_true_arbs(
        _kp() + _ps(), bankroll=Decimal("1000"), max_stake_pct=Decimal("0.10"),
        trusted_pairs=WHITELIST, stale_warn=Decimal("1"), doubleheader_cluster_tol_secs=7200,
    )
    by_pair = {venue_pair_key(o.legs): o.status for o in arbs}
    assert by_pair["kalshi+polymarket"] == "OK"     # whitelisted → bookable into the clean lane
    assert by_pair["polymarket+sx"] == "REVIEW"      # not whitelisted → research only
