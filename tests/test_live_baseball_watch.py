"""LIVE_BASEBALL_EV Phase 1 — candidate recorder gates + recheck + measurement-only integrity.
Fixtures/synthetic snapshots only, no network."""
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from arb.experimental.live_baseball_ev import LiveSnapshot, evaluate_live_candidate
from arb.experimental.live_baseball_watch import (
    GameTracker, record_candidate, run_recheck, watch_once, watch_report,
)
from arb.store import Store

CFG = {"max_state_age_secs": 45, "max_venue_age_secs": 5, "max_consensus_age_secs": 5,
       "mlb_publish_lag_secs": 43, "min_live_books": 3, "live_ev_min_edge": 0.07,
       "min_depth_usd": 50, "post_event_freeze_secs": 10}


def _stable(**kw) -> LiveSnapshot:
    """A fresh, stable, priced snapshot that (absent overrides) is a FRESH_CANDIDATE."""
    base = dict(event_key="baseball_mlb:2026-07-02:cincinnati reds:milwaukee brewers",
                venue="kalshi", book="KXMLB", outcome_label="milwaukee brewers",
                executable_cost=Decimal("0.50"), depth_usd=Decimal("600"),
                fair_value=Decimal("0.62"), fair_value_lo=Decimal("0.60"), fair_value_hi=Decimal("0.64"),
                n_books=3, venue_age_secs=1.0, state_age_secs=2.0, consensus_age_secs=1.0,
                secs_since_play=30.0, secs_since_runner_change=30.0, secs_since_score_change=30.0,
                at_bat_live=False, inning=7, half="Bottom", outs=1, bases="1",
                score={"home": 2, "away": 6})
    base.update(kw)
    return LiveSnapshot(**base)


def test_stable_between_batters_snapshot_is_a_fresh_candidate() -> None:
    d = evaluate_live_candidate(_stable(), CFG)
    assert d.status == "FRESH_CANDIDATE" and d.raw_edge > 0


def test_mid_play_snapshot_rejected_unstable() -> None:
    d = evaluate_live_candidate(_stable(at_bat_live=True, count_age_secs=30.0, pitch_clock_secs=20.0), CFG)
    assert d.status == "REJECTED_UNSTABLE" and d.reason == "live-at-bat-stale-count"


def test_stale_state_snapshot_rejected() -> None:
    d = evaluate_live_candidate(_stable(state_age_secs=90.0), CFG)  # > 45s gate
    assert d.status == "REJECTED_STALE" and d.reason == "stale-game-state"


def test_suspended_market_snapshot_rejected() -> None:
    d = evaluate_live_candidate(_stable(suspended=True), CFG)
    assert d.status == "REJECTED_STALE" and d.reason == "suspended-or-cooldown"


def test_post_event_freeze_rejected() -> None:
    d = evaluate_live_candidate(_stable(secs_since_score_change=3.0), CFG)
    assert d.status == "REJECTED_UNSTABLE"


def test_recheck_vanished_on_moved_price() -> None:
    entry = evaluate_live_candidate(_stable(), CFG)
    assert entry.status == "FRESH_CANDIDATE"
    moved = _stable(executable_cost=Decimal("0.63"))   # price ran past fair value -> edge gone
    assert run_recheck(entry, lambda: moved, CFG) == "LIVE_VANISHED"


def test_recheck_confirms_when_unchanged() -> None:
    entry = evaluate_live_candidate(_stable(), CFG)
    assert run_recheck(entry, lambda: _stable(), CFG) == "CONFIRMED"


def test_recheck_must_refetch_reusing_entry_is_an_error() -> None:
    entry = evaluate_live_candidate(_stable(), CFG)
    calls = []

    def refetch():
        calls.append(1)
        return _stable()
    run_recheck(entry, refetch, CFG)
    assert calls == [1]                       # the recheck MUST have re-fetched
    # Returning the entry snapshot object itself is forbidden (§1.10 — no reuse).
    with pytest.raises(ValueError):
        run_recheck(entry, lambda: entry.snapshot, CFG)


def test_ended_game_produces_nothing(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")

    class FakeMlb:
        def live_feed(self, game_pk):
            return {"gameData": {"status": {"abstractGameState": "Final"},
                                 "teams": {"home": {"name": "Milwaukee Brewers"}, "away": {"name": "Cincinnati Reds"}},
                                 "game": {"pk": game_pk}},
                    "liveData": {"linescore": {}, "plays": {"currentPlay": {}}}}
    games = [{"game_pk": 823765, "home": "Milwaukee Brewers", "away": "Cincinnati Reds"}]
    counts = watch_once(store, SimpleNamespace(live_baseball_ev=CFG), FakeMlb(), [], games, {}, set())
    assert counts["ended"] == 1 and counts["recorded"] == 0
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM live_candidates").fetchone()[0] == 0


def test_records_to_live_candidates_never_flat_bets(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    d = evaluate_live_candidate(_stable(), CFG)
    record_candidate(store, d, recheck_result="CONFIRMED", game_pk=823765, no_consensus=False)
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM live_candidates").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM flat_bets WHERE kind='LIVE_BASEBALL_EV'").fetchone()[0] == 0
    assert "MUST be 0): 0" in watch_report(store)


def test_no_consensus_snapshot_is_research(tmp_path) -> None:
    # fair_value=None (the Phase-0 credit reality) -> n_books 0 -> RESEARCH (NO-CONSENSUS), never EV.
    d = evaluate_live_candidate(_stable(fair_value=None, fair_value_lo=None, fair_value_hi=None, n_books=0), CFG)
    assert d.status == "RESEARCH" and d.reason == "under-min-books"
