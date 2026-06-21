from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.experimental.mm_sim import MMScenario, simulate, yield_curve
from arb.experimental.newslag import clv_after_lag, lag_summary, measure_lag

BASE = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def at(secs, price):
    return (BASE + timedelta(seconds=secs), price)


# ---- S4 news-lag ----

def test_venue_lags_reference_signal() -> None:
    reference = [at(0, 100.0), at(10, 100.0), at(20, 105.0)]      # +5 move at t=20
    venue = [at(0, 0.50), at(25, 0.50), at(30, 0.58)]            # venue moves up at t=30 (lag 10s)
    samples = measure_lag(reference, venue, ref_threshold=2.0, venue_threshold=0.05, max_lag_secs=60)
    assert len(samples) == 1
    assert samples[0].direction == 1
    assert samples[0].venue_lag_secs == 10.0
    assert lag_summary(samples)["median_lag_secs"] == 10.0


def test_no_following_move_no_sample() -> None:
    reference = [at(0, 100.0), at(20, 105.0)]
    venue = [at(0, 0.50), at(5, 0.50)]   # venue never moves after the signal
    assert measure_lag(reference, venue, 2.0, 0.05, 60) == []


def test_clv_after_lag_sign() -> None:
    # signal up; you fill at 0.55, venue keeps rising to... measured by fill vs signal price.
    assert clv_after_lag(0.55, 0.60, 1) == 0.05      # kept moving your way -> capturable
    assert clv_after_lag(0.55, 0.52, 1) == -0.03     # reverted -> too late


# ---- S6 MM simulator ----

def test_mm_sim_separates_adverse_selection() -> None:
    s = MMScenario(size_usd=Decimal("100"), inside_spread_cents=1, fill_prob=Decimal("0.5"),
                   spread_cents=3, reward_rate_daily=Decimal("0.001"),
                   adverse_run_prob=Decimal("0.3"), adverse_loss_cents=5)
    r = simulate(s)
    # captured = 0.5*100*0.02 = 1.00 ; reward = 0.001*100 = 0.10 ; adverse = 0.5*0.3*100*0.05 = 0.75
    assert r["captured_spread"] == "1.00"
    assert r["reward"] == "0.10"
    assert r["adverse_selection"] == "-0.75"
    assert r["expected_daily_pnl"] == "0.35"
    assert "SIMULATION" in r["note"]


def test_mm_yield_curve_grid() -> None:
    base = MMScenario(Decimal("50"), 1, Decimal("0.4"), 3, Decimal("0.001"), Decimal("0.3"), 5)
    rows = yield_curve([Decimal("50"), Decimal("100")], [1, 2], base)
    assert len(rows) == 4
    assert all("SIMULATION" in r["note"] for r in rows)
