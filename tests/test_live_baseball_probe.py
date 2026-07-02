"""LIVE_BASEBALL_EV Phase 0 probe — parsing + verdict logic. Fixtures only, no network."""
from datetime import datetime, timezone
from pathlib import Path

from arb.experimental.live_baseball_probe import (
    _pcts, _verdict, credit_math, is_terminal, parse_live_feed, publish_lag_secs,
)
from arb.venues.mlbstats import MlbStatsAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def _state():
    return parse_live_feed(MlbStatsAdapter(FIXTURES).live_feed(823765))


def test_parse_live_feed_extracts_state() -> None:
    s = _state()
    assert s.game_pk == 823765 and s.status == "Live"
    assert s.inning == 7 and s.half == "Bottom" and s.outs == 1
    assert s.home_score == 2 and s.away_score == 6
    assert s.bases == "1"                      # runner on first only
    assert s.away == "Cincinnati Reds" and s.home == "Milwaukee Brewers"
    assert s.publish_time == datetime(2026, 7, 2, 20, 8, 45, tzinfo=timezone.utc)


def test_publish_lag_is_our_clock_minus_feed_time() -> None:
    s = _state()
    fetched = datetime(2026, 7, 2, 20, 8, 49, tzinfo=timezone.utc)  # 4s after the feed stamp
    assert publish_lag_secs(s, fetched) == 4.0
    # A feed with no timestamp yields None, not a fabricated lag.
    from dataclasses import replace
    assert publish_lag_secs(replace(s, publish_time=None), fetched) is None


def test_is_terminal() -> None:
    assert is_terminal("Final") and is_terminal("Game Over")
    assert not is_terminal("Live") and not is_terminal("Preview")


def test_credit_math_states_the_blocker() -> None:
    text = credit_math({"probe_poll_secs": 10, "max_odds_api_calls_per_day": 20})
    assert "contradictory" in text and "500/mo" in text


def test_pcts() -> None:
    assert _pcts([]) == {}
    p = _pcts([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert p["n"] == 10 and p["min"] == 1 and p["max"] == 10 and p["p50"] >= 5


def test_verdict_flags_unachievable_state_gate() -> None:
    # publish lag p50 well above the 5s gate => NOT ACHIEVABLE.
    lag_p = {"n": 100, "p50": 9.0, "p90": 14.0, "p99": 20.0, "min": 3.0, "max": 25.0}
    depth_p = {"n": 50, "p50": 12.0, "p90": 40.0, "p99": 60.0, "min": 5.0, "max": 80.0}
    out = "\n".join(_verdict(lag_p, depth_p, depth_over_50=18.0, books=[1, 2, 2],
                             max_state=5, min_depth=50))
    assert "NOT ACHIEVABLE" in out               # state gate
    assert "MICRO lane in disguise" in out       # $50 rarely met
    assert "rarely" in out or "unavailable" in out  # <3 live books


def test_verdict_confirms_achievable_when_fresh_and_deep() -> None:
    lag_p = {"n": 100, "p50": 2.0, "p90": 4.0, "p99": 5.0, "min": 1.0, "max": 6.0}
    depth_p = {"n": 50, "p50": 120.0, "p90": 300.0, "p99": 500.0, "min": 60.0, "max": 800.0}
    out = "\n".join(_verdict(lag_p, depth_p, depth_over_50=85.0, books=[3, 4, 3],
                             max_state=5, min_depth=50))
    assert "ACHIEVABLE" in out and "$50 lane is real" in out


def test_verdict_insufficient_without_lag_samples() -> None:
    assert "INSUFFICIENT DATA" in "\n".join(_verdict({}, {}, 0.0, [], 5, 50))
