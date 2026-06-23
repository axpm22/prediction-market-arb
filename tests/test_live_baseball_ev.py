from dataclasses import replace
from decimal import Decimal

from arb.config import load_config
from arb.experimental.live_baseball_ev import (
    LiveSnapshot, evaluate_live_candidate, record_live_candidate, recheck,
)
from arb.store import Store

CFG = load_config().live_baseball_ev


def snap(**over) -> LiveSnapshot:
    base = dict(
        event_key="baseball_mlb:2026-06-20:athletics:mariners", venue="kalshi", book="KX",
        outcome_label="mariners", executable_cost=Decimal("0.45"), exit_bid_net=Decimal("0.44"),
        depth_usd=Decimal("200"), fair_value=Decimal("0.60"), fair_value_lo=Decimal("0.60"),
        fair_value_hi=Decimal("0.60"), n_books=3, venue_age_secs=1.0, state_age_secs=1.0,
        consensus_age_secs=1.0, inning=5, half="top", outs=1, bases="000", score={"mariners": 2, "athletics": 1},
        at_bat_live=False, count_age_secs=1.0,
    )
    base.update(over)
    return LiveSnapshot(**base)


def test_promoted_fresh_candidate() -> None:
    d = evaluate_live_candidate(snap(), CFG)
    assert d.status == "FRESH_CANDIDATE"
    assert d.raw_edge == Decimal("0.150000")          # 0.60 - 0.45
    assert d.adjusted_edge == Decimal("0.115000")     # - 0.03 latency - 0.005 fee - 0 ci
    assert d.promoted is True                          # 0.115 >= 0.07 adjusted bar


def test_stale_venue_quote_rejected() -> None:
    d = evaluate_live_candidate(snap(venue_age_secs=9.0), CFG)
    assert d.status == "REJECTED_STALE" and d.reason == "stale-venue-quote"


def test_post_event_freeze_rejected() -> None:
    d = evaluate_live_candidate(snap(secs_since_play=2.0), CFG)
    assert d.status == "REJECTED_UNSTABLE" and d.reason == "post-event-freeze"


def test_under_min_books_is_research_not_ev() -> None:
    d = evaluate_live_candidate(snap(n_books=2), CFG)
    assert d.status == "RESEARCH"


def test_ci_width_widens_uncertainty_buffer() -> None:
    wide = evaluate_live_candidate(snap(fair_value_lo=Decimal("0.55"), fair_value_hi=Decimal("0.65")), CFG)
    # 0.10 CI width * 0.5 = 0.05 extra buffer -> adjusted 0.115 - 0.05 = 0.065 < 0.07 bar.
    assert wide.adjusted_edge == Decimal("0.065000")
    assert wide.promoted is False


def test_recheck_confirmed_then_vanished() -> None:
    entry = evaluate_live_candidate(snap(), CFG)
    assert recheck(entry, snap(), CFG) == "CONFIRMED"
    assert recheck(entry, snap(outs=2), CFG) == "LIVE_VANISHED"               # state changed
    assert recheck(entry, snap(executable_cost=Decimal("0.62")), CFG) == "LIVE_VANISHED"  # edge gone
    assert recheck(entry, snap(venue_age_secs=9.0), CFG) == "LIVE_VANISHED"   # refetch stale


def test_record_isolates_lane_and_quarantines_research(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("live")
    confirmed_id = record_live_candidate(store, poll, evaluate_live_candidate(snap(), CFG), "CONFIRMED")
    research_id = record_live_candidate(store, poll, evaluate_live_candidate(snap(n_books=2), CFG), None)
    with store.connect() as con:
        rows = {r["id"]: r for r in con.execute("SELECT * FROM flat_bets WHERE kind='LIVE_BASEBALL_EV'")}
    assert rows[confirmed_id]["status"] == "OPEN" and rows[confirmed_id]["quality_flag"] is None
    assert rows[research_id]["status"] == "CLOSED" and rows[research_id]["quality_flag"] == "live-research"
    # live lane is its own kind, never folded into EV/ARB totals.
    assert all(r["kind"] == "LIVE_BASEBALL_EV" for r in rows.values())
