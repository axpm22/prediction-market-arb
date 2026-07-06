"""MODEL_FOUNDATIONS Phase D — heartbeat/staleness/single-run lock (fixtures-free)."""
import os
from datetime import datetime, timedelta, timezone

from arb.heartbeat import (
    acquire_lock, beat, is_stale, pid_alive, read_heartbeats,
)
from arb.store import Store

NOW = datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc)


def test_beat_upserts_and_read(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    beat(store, "watch", pid=123, polls_done=1, interval_secs=60, head="abc", dirty=True, now=NOW)
    beat(store, "watch", pid=123, polls_done=2, interval_secs=60, head="abc", dirty=False, now=NOW + timedelta(seconds=60))
    rows = read_heartbeats(store)
    assert len(rows) == 1                       # UPSERT, not a second row
    assert rows[0]["polls_done"] == 2 and rows[0]["dirty"] == 0


def test_is_stale_by_interval(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    beat(store, "watch", pid=1, interval_secs=60, now=NOW)
    hb = read_heartbeats(store)[0]
    assert not is_stale(hb, now=NOW + timedelta(seconds=120))     # < 3x interval
    assert is_stale(hb, now=NOW + timedelta(seconds=200))         # > 3x60s


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    assert pid_alive(2_000_000_000) is False


def test_single_run_lock(tmp_path):
    lock_dir = tmp_path / ".locks"
    assert acquire_lock("watch", pid=os.getpid(), lock_dir=lock_dir) is True
    # a DIFFERENT, live pid already holds it -> refuse to double-run
    assert acquire_lock("watch", pid=os.getpid() + 1, lock_dir=lock_dir) is False
    # a stale lock (dead holder) is reclaimed
    (lock_dir / "focus.pid").write_text("2000000000")
    assert acquire_lock("focus", pid=os.getpid(), lock_dir=lock_dir) is True


def test_supervisor_stop_requested_honors_flags(tmp_path, monkeypatch) -> None:
    # 03: the supervisor must honor stop-all / stop-supervisor (the documented safety control) —
    # its one-shot watch children can't check it themselves.
    from arb.heartbeat import supervisor_stop_requested
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    assert supervisor_stop_requested() is False
    (tmp_path / "data" / "stop-all").write_text("stop")
    assert supervisor_stop_requested() is True
    (tmp_path / "data" / "stop-all").unlink()
    (tmp_path / "data" / "stop-supervisor").write_text("stop")
    assert supervisor_stop_requested() is True


def test_format_status_one_shot_child_states(tmp_path) -> None:
    # status.sh fix: under the supervisor, watch is a one-shot `watch --once` relaunch loop —
    # its recorded child pid is dead between cycles. pid-death alone must NEVER read DEAD.
    from datetime import timedelta

    from arb.heartbeat import format_status
    from arb.store import Store

    store = Store(tmp_path / "arb.sqlite")
    dead_pid = 2000000000  # guaranteed-dead pid (same convention as the lock test)
    beat(store, "watch", pid=dead_pid, polls_done=5, interval_secs=60, head="abc",
         dirty=False, now=NOW)
    hb = read_heartbeats(store)

    # fresh beat + dead pid -> ALIVE (between relaunches)
    out = format_status(hb, now=NOW + timedelta(seconds=90), supervisor_up=True)
    assert "ALIVE" in out and "DEAD" not in out
    # stale beat + dead pid + supervisor lock held -> STALE (it will relaunch)
    out = format_status(hb, now=NOW + timedelta(seconds=600), supervisor_up=True)
    assert "STALE" in out and "DEAD" not in out
    # stale beat + dead pid + no supervisor -> DEAD (the only true-dead case)
    out = format_status(hb, now=NOW + timedelta(seconds=600), supervisor_up=False)
    assert "DEAD" in out
    # live pid (an in-progress cycle) is ALIVE even if the beat is stale (beats land at cycle end)
    beat(store, "watch", pid=os.getpid(), polls_done=6, interval_secs=60, head="abc",
         dirty=False, now=NOW)
    out = format_status(read_heartbeats(store), now=NOW + timedelta(seconds=600),
                        supervisor_up=False)
    assert "ALIVE" in out
    # columns no longer collide: pid and code are separated by a space
    assert f"{os.getpid()} abc" in out


def test_supervisor_alive_reads_lock(tmp_path) -> None:
    from arb.heartbeat import supervisor_alive

    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    assert supervisor_alive(lock_dir=lock_dir) is False          # no lock file
    (lock_dir / "supervisor.pid").write_text("2000000000")
    assert supervisor_alive(lock_dir=lock_dir) is False          # dead holder
    (lock_dir / "supervisor.pid").write_text(str(os.getpid()))
    assert supervisor_alive(lock_dir=lock_dir) is True           # live holder


def test_data_stale_alarm(tmp_path) -> None:
    # 2026-07-06: the alarm that would have made the silent Jul 4-6 outage visible.
    from datetime import datetime, timedelta, timezone
    from arb.heartbeat import data_stale_alarm, newest_quote_age_secs
    store = Store(tmp_path / "arb.sqlite")
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    # no quotes yet → alarm
    assert "no quotes" in data_stale_alarm(store, 900, now=now)
    # fresh quote → no alarm
    with store.connect() as con:
        con.execute("INSERT INTO quotes(fetched_at, event_key, category, sport, venue, book, "
                    "market_type, outcome, raw_price, cost, fee_cost, live) "
                    "VALUES (?, 'e', 'sports', 'baseball_mlb', 'kalshi', 'b', 'WINNER', 'a', '0.5','0.5','0',0)",
                    ((now - timedelta(seconds=60)).isoformat(),))
    assert data_stale_alarm(store, 900, now=now) is None
    assert 55 < newest_quote_age_secs(store, now=now) < 65
    # stale quote (2 days old) → loud alarm
    assert "STALLED" in data_stale_alarm(store, 900, now=now + timedelta(days=2))
