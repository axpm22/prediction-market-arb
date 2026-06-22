import json
from datetime import datetime, timezone
from decimal import Decimal

from arb.bias import _pre_fee_cost, calibration
from arb.btc_arb import BtcLeg, find_btc_arb
from arb.models import Event, Opportunity, Quote
from arb.store import Store
from arb.tracker import _local_date, _bet_records_for_date
from arb.validation import ValidationRule, classify_validation


# ---- #1 EST-day attribution ----

def test_late_utc_bet_attributes_to_prior_est_day() -> None:
    # 2026-06-11 02:30 UTC == 2026-06-10 22:30 EDT -> belongs to June 10, not June 11.
    assert _local_date("2026-06-11T02:30:00+00:00") == datetime(2026, 6, 10).date()
    assert _local_date("2026-06-11T18:00:00+00:00") == datetime(2026, 6, 11).date()


def _open_bet(con, placed_at: str) -> None:
    con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, optimistic_cost, status, placed_at, notes)
           VALUES (?, 1, 'sports', 'baseball_mlb', 'EV', 1, 0, '0.03', '1.00', '[]', '0.50', 'OPEN', ?, '')""",
        (f"fp{placed_at}", placed_at),
    )


def test_daily_records_bucket_by_est_date(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        _open_bet(con, "2026-06-11T02:30:00+00:00")  # EDT June 10
        _open_bet(con, "2026-06-11T18:00:00+00:00")  # EDT June 11
    june10 = _bet_records_for_date(store, datetime(2026, 6, 10).date())
    june11 = _bet_records_for_date(store, datetime(2026, 6, 11).date())
    assert len(june10) == 1 and len(june11) == 1


# ---- #3 FLB uses pre-fee cost + mean cost in band ----

def test_pre_fee_cost_prefers_leg_cost_over_fee_loaded() -> None:
    legs = json.dumps([{"cost": "0.50"}])
    assert _pre_fee_cost(legs, "0.5175") == 0.50      # leg pre-fee cost, not optimistic
    assert _pre_fee_cost("[]", "0.5175") == 0.5175    # falls back when no leg cost


def test_calibration_edge_is_vs_mean_cost(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    legs = json.dumps([{"cost": "0.45"}])
    with store.connect() as con:
        for i in range(10):
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, optimistic_cost, status, placed_at, result, quality_flag, notes)
                   VALUES (?, 1, 'sports', 'baseball_mlb', 'EV', 1, 0, '0', '1', ?, '0.55', 'SETTLED', ?, ?, NULL, '')""",
                (f"fp{i}", legs, datetime.now(timezone.utc).isoformat(), "WON" if i < 5 else "LOST"),
            )
    [band] = [b for b in calibration(store, [0.4, 0.5], min_samples=1, flb_min_edge=0.02) if b.n]
    assert band.mean_cost == 0.45              # binned on pre-fee 0.45, not optimistic 0.55
    assert band.realized_win_rate == 0.5
    assert band.edge == round(0.5 - 0.45, 4)   # realized minus MEAN cost, not midpoint 0.45->0.45


# ---- #5 validation gates on probability points ----

def _ev_opp(cost: str, edge_ratio: str, edge_pts: str) -> Opportunity:
    q = Quote(event_key="e", venue="kalshi", book="b", market_type="WINNER", line=None,
              outcome_label="x", raw_price=Decimal(cost), cost=Decimal(cost), fee_cost=Decimal("0"),
              depth_usd=Decimal("500"), live=False, fetched_at=datetime.now(timezone.utc))
    ev = Event(event_key="e", category="sports", sport="baseball_mlb", home="a", away="b",
               start_utc=datetime.now(timezone.utc))
    return Opportunity(kind="EV", category="sports", event=ev, legs=[q],
                       edge=Decimal(edge_ratio), edge_points=Decimal(edge_pts), stakes={}, status="REVIEW")


def test_validation_keeps_genuine_2pt_edge_at_mid_cost() -> None:
    # cost 0.50, true edge 1.5 pts (<2pt rule) -> ratio 0.03 would have been DROPPED before.
    opp = _ev_opp(cost="0.50", edge_ratio="0.03", edge_pts="0.015")
    rule_id, tag, _ = classify_validation(opp, "CONFIRMED", ValidationRule())
    assert tag == "KEEP"


def test_validation_drops_large_points_edge() -> None:
    opp = _ev_opp(cost="0.50", edge_ratio="0.10", edge_pts="0.05")
    _, tag, notes = classify_validation(opp, "CONFIRMED", ValidationRule())
    assert tag == "DROP" and "validation-drop:flagged-edge" in notes


# ---- #6 one best cross-venue BTC pair per window ----

def test_btc_emits_one_best_pair_per_window() -> None:
    W = datetime(2026, 6, 21, 11, 0, tzinfo=timezone.utc)
    legs = [
        BtcLeg("kalshi", "BTC", "UP", W, Decimal("0.50"), "idx"),
        BtcLeg("polymarket", "BTC", "UP", W, Decimal("0.46"), "idx"),   # cheaper UP
        BtcLeg("sx", "BTC", "DOWN", W, Decimal("0.49"), "idx"),
    ]
    arbs = find_btc_arb(legs, window_match_secs=5, trusted=False)
    assert len(arbs) == 1                              # not 2 (was one per UPxDOWN combo)
    assert arbs[0].up_leg.venue == "polymarket"        # cheapest UP chosen
    assert arbs[0].total_cost == Decimal("0.950000")
