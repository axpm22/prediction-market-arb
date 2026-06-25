"""C1 simultaneity haircut + D1/D2 clustering vectors (ARB_LADDER_AND_FRICTIONS_SPEC §1/§4/§5)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.arbitrage import _hold_days, detect_true_arbs
from arb.models import Quote
from arb.pricing import q6

T0 = datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc)
START = datetime(2026, 6, 20, 23, 5, tzinfo=timezone.utc)
VELOCITY = {"default": Decimal("0.001"), "baseball_mlb": Decimal("0.0008")}


def leg(outcome, cost, *, fetched=T0, start=START, venue="kalshi"):
    return Quote(
        event_key="baseball_mlb:2026-06-20:los angeles dodgers:new york yankees",
        venue=venue, book=f"{venue}-{outcome}", market_type="WINNER", line=None,
        outcome_label=outcome, raw_price=Decimal(cost), cost=Decimal(cost),
        fee_cost=Decimal("0"), depth_usd=Decimal("500"), live=False,
        fetched_at=fetched, start_utc=start,
        home="los angeles dodgers", away="new york yankees",
    )


def _detect(legs, *, stale_warn=Decimal("0.04"), **kw):
    return detect_true_arbs(legs, bankroll=Decimal("1000"), max_stake_pct=Decimal("0.10"),
                            trusted_pairs=set(), stale_warn=stale_warn, **kw)


def _c1(legs):
    # stale_warn high so the (separate) likely-stale roi gate never masks the C1 behavior.
    return _detect(legs, stale_warn=Decimal("1"), line_velocity=VELOCITY, max_actionable_skew_secs=10,
                   min_adj_edge_points=Decimal("0.002"), doubleheader_cluster_tol_secs=7200)


def _note(opp, needle):
    return any(needle in n for n in opp.notes)


# ---- C1 simultaneity haircut ----

def test_c1_simultaneous_full_edge_is_actionable():
    # 0s apart, 5-pt edge (single-venue → no settlement-rule REVIEW): stays OK, adj_edge == edge.
    opp = _c1([leg("los angeles dodgers", "0.475"), leg("new york yankees", "0.475")])[0]
    assert opp.status == "OK"
    assert _note(opp, "adj_edge=0.050000") and _note(opp, "skew=0s")


def test_c1_small_skew_flags_but_stays_actionable():
    # 8s apart (≤10), v_mlb=0.0008 → haircut 0.0064, adj_edge 0.0436 (> floor): flagged, still OK.
    opp = _c1([leg("los angeles dodgers", "0.475"),
               leg("new york yankees", "0.475", fetched=T0 + timedelta(seconds=8))])[0]
    assert opp.status == "OK"
    assert _note(opp, "skew=8s") and _note(opp, "adj_edge=0.043600")
    assert not _note(opp, "skew-nonsimultaneous") and not _note(opp, "skew-edge-eroded")


def test_c1_large_skew_is_review_nonsimultaneous():
    # 120s apart > 10s actionable window → never live together → REVIEW.
    opp = _c1([leg("los angeles dodgers", "0.475"),
               leg("new york yankees", "0.475", fetched=T0 + timedelta(seconds=120))])[0]
    assert opp.status == "REVIEW" and _note(opp, "skew-nonsimultaneous")


def test_c1_tiny_edge_eroded_by_moderate_skew_is_review():
    # 0.6-pt edge, 8s skew, haircut 0.0064 → adj_edge < 0: REVIEW skew-edge-eroded.
    opp = _c1([leg("los angeles dodgers", "0.497"),
               leg("new york yankees", "0.497", fetched=T0 + timedelta(seconds=8))])[0]
    assert opp.status == "REVIEW" and _note(opp, "skew-edge-eroded")
    assert not _note(opp, "skew-nonsimultaneous")


# ---- D2 doubleheader clustering ----

def _doubleheader_legs():
    g1, g2 = START, START.replace(hour=23) + timedelta(hours=6)  # ~6h apart > 2h tol
    return [
        leg("los angeles dodgers", "0.45", start=g1), leg("new york yankees", "0.60", start=g1),
        leg("los angeles dodgers", "0.60", start=g2), leg("new york yankees", "0.45", start=g2),
    ]


def test_d2_clustering_prevents_cross_game_arb():
    # Without clustering the date-keyed group fakes a 0.90 lock by pairing g1-home with g2-away —
    # the arb is DETECTED (recorded) even if REVIEW. That detection is the bug.
    fake = _detect(_doubleheader_legs(), doubleheader_cluster_tol_secs=None)
    assert any(o.edge_points > 0 for o in fake)  # a cross-game lock was detected
    # Clustering splits the two games; each is 1.05 → no arb detected at all.
    assert _detect(_doubleheader_legs(), doubleheader_cluster_tol_secs=7200) == []


def test_d2_single_game_still_detected():
    legs = [leg("los angeles dodgers", "0.45"),
            leg("new york yankees", "0.45", fetched=T0 + timedelta(seconds=30))]
    assert len(_detect(legs, doubleheader_cluster_tol_secs=7200)) == 1


# ---- D1 start-unknown legs are non-actionable under clustering ----

def test_d1_untimed_leg_excluded_from_actionable_arbs():
    legs = [leg("los angeles dodgers", "0.45", start=None),  # start-unknown → dropped
            leg("new york yankees", "0.45")]
    assert _detect(legs, doubleheader_cluster_tol_secs=7200) == []
    # legacy (no clustering) would still pair them into a fake lock — proves clustering is the gate
    assert len(_detect(legs, doubleheader_cluster_tol_secs=None)) == 1


# ---- C4 holding period charged to settlement, not event start ----

def test_c4_hold_days_uses_slowest_venue_settlement():
    now = datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc)
    start = now + timedelta(days=14)
    legs = [leg("los angeles dodgers", "0.50", start=start, venue="kalshi"),
            leg("new york yankees", "0.50", start=start, venue="polymarket")]
    dur = {"baseball_mlb": Decimal("3.5")}
    lag = {"kalshi": Decimal("24"), "polymarket": Decimal("48")}
    hd = _hold_days(legs, now, dur, lag)
    # capital releases only when the SLOWER venue (polymarket, 48h) settles: start + 3.5 + 48h.
    expected = q6(Decimal(str((start + timedelta(hours=51.5) - now).total_seconds())) / Decimal("86400"))
    assert hd == expected


def test_c4_settlement_horizon_flips_far_dated_lock_to_uneconomic():
    start = datetime.now(timezone.utc) + timedelta(days=14)
    legs = [leg("los angeles dodgers", "0.499", start=start, venue="kalshi"),
            leg("new york yankees", "0.499", start=start, venue="polymarket")]
    dur = {"baseball_mlb": Decimal("3.5")}
    lag = {"kalshi": Decimal("24"), "polymarket": Decimal("48")}
    # ~0.2% lock 14 days out: economic on the start-based horizon, uneconomic once charged to
    # the polymarket settlement (game end + 48h pushes the hold past the charge break-even).
    start_based = _detect(legs, capital_charge_annual=Decimal("0.05"))
    settle_based = _detect(legs, capital_charge_annual=Decimal("0.05"),
                           sport_duration_hours=dur, venue_settlement_lag_hours=lag)
    assert not any("uneconomic" in o.notes for o in start_based)
    assert any("uneconomic" in o.notes for o in settle_based)
