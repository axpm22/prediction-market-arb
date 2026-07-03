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
