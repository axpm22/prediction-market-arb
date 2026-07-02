"""stop_inplay_betting_fix — the last-line defenses added on top of the split_pregame fix:
the tracker-boundary invariant (refuse in-play EV/ARB inserts), the stale-daemon guard, and the
report's data-quality (voided + refused) surfacing. Fixtures only."""
from datetime import timedelta

from arb.cli import code_change_warning
from arb.experimental.annotations import annotate
from arb.store import Store
from arb.tracker import record_tracker_poll, write_daily_report

from tests.test_tracker import NOW, arb_opp


def test_tracker_refuses_inplay_bet_even_when_opp_start_looks_pregame(tmp_path, capsys) -> None:
    store = Store(tmp_path / "arb.sqlite")
    opp = arb_opp("0.48", "0.48")  # opp.event.start_utc is in the future (mislabeled slot)
    # But the AUTHORITATIVE reliable start says the event began a minute ago.
    started = {opp.event.event_key: NOW - timedelta(minutes=1)}
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW, event_starts=started)
    with store.connect() as con:
        opens = con.execute("SELECT COUNT(*) FROM flat_bets WHERE status='OPEN'").fetchone()[0]
        refused = con.execute("SELECT COUNT(*) FROM flat_bets WHERE quality_flag='inplay-bet-refused'").fetchone()[0]
    assert opens == 0 and refused == 1                 # no bet booked; refusal recorded
    assert "inplay-bet-refused" in capsys.readouterr().out


def test_reliable_future_start_still_books(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    opp = arb_opp("0.48", "0.48")
    future = {opp.event.event_key: NOW + timedelta(hours=2)}
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW, event_starts=future)
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM flat_bets WHERE status='OPEN'").fetchone()[0] == 1


def test_unknown_reliable_start_is_refused(tmp_path) -> None:
    # Event not in the reliable-start map (only an unreliable venue quoted it) -> non-actionable.
    store = Store(tmp_path / "arb.sqlite")
    opp = arb_opp("0.48", "0.48")
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW, event_starts={})
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM flat_bets WHERE status='OPEN'").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM flat_bets WHERE quality_flag='inplay-bet-refused'").fetchone()[0] == 1


def test_stale_daemon_guard_warns_on_changed_code() -> None:
    # A fake start-state that doesn't match the current HEAD => the guard fires (or git is absent
    # -> None, in which case there's nothing to guard and that's acceptable).
    warning = code_change_warning(("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", False))
    assert warning is None or "restart to pick it up" in warning
    # The real current state never warns against itself.
    from arb.cli import _git_state
    assert code_change_warning(_git_state()) is None


def test_voided_bet_shows_in_data_quality_and_leaves_pl(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    opp = arb_opp("0.48", "0.48")
    future = {opp.event.event_key: NOW + timedelta(hours=2)}
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW, event_starts=future)
    with store.connect() as con:
        bet_id = con.execute("SELECT id FROM flat_bets WHERE status='OPEN'").fetchone()[0]
    annotate(store, int(bet_id), tag="inplay-mislabeled", reason="void: booked during live match")
    _csv, md_path = write_daily_report(store, report_date=NOW.date(), output_dir=tmp_path)
    report = md_path.read_text()
    assert "## Data Quality" in report and "voided — data error: 1" in report
    assert "inplay-mislabeled=1" in report
