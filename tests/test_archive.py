"""H8 — `arb archive` moves quotes older than archive_after_days into monthly
archive DBs, verifies them present, then deletes from the hot DB. flat_bets /
bet_events are never touched (rule 9); rule 7 is honored by preserving every
quote in the archive."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arb.config import load
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


def _q(store, at):
    store.insert("quotes", {"fetched_at": at, "event_key": "e", "poll_id": 1,
                            "venue": "kalshi", "cost": "0.5", "ladder_json": "[]"})


def test_archive_moves_only_old_quotes_and_preserves_them(cfg, tmp_path):
    from arb.archive import run_archive
    store = Store(tmp_path / "hot.sqlite")
    days = int(cfg["archive_after_days"])
    old = (NOW - timedelta(days=days + 3)).isoformat()     # this month, > cutoff old
    older = (NOW - timedelta(days=days + 40)).isoformat()  # a previous month
    fresh = (NOW - timedelta(hours=1)).isoformat()         # inside the hot window
    for at in (old, old, older, fresh, fresh):
        _q(store, at)

    res = run_archive(cfg, store, archive_dir=tmp_path / "archive", now=NOW)

    assert res["moved"] == 3
    assert store.query("SELECT COUNT(*) c FROM quotes")[0]["c"] == 2  # fresh remain
    cutoff = (NOW - timedelta(days=days)).isoformat()
    assert store.query("SELECT COUNT(*) c FROM quotes WHERE fetched_at < ?",
                       (cutoff,))[0]["c"] == 0                        # no old left hot
    # every archived quote is preserved in a monthly archive DB (rule 7)
    total = 0
    for m in res["months"]:
        a = sqlite3.connect(tmp_path / "archive" / f"arb-{m}.sqlite")
        total += a.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        a.close()
    assert total == 3


def test_archive_noop_when_nothing_old(cfg, tmp_path):
    from arb.archive import run_archive
    store = Store(tmp_path / "hot.sqlite")
    _q(store, (NOW - timedelta(hours=2)).isoformat())
    res = run_archive(cfg, store, archive_dir=tmp_path / "archive", now=NOW)
    assert res["moved"] == 0
    assert store.query("SELECT COUNT(*) c FROM quotes")[0]["c"] == 1


def test_backup_prune_keeps_only_newest(cfg, tmp_path):
    from arb.commands import _prune_backups
    # create 5 backup files with increasing mtimes
    for i in range(5):
        f = tmp_path / f"arb-2026070{i}.sqlite"
        f.write_bytes(b"x")
        import os
        os.utime(f, (1000 + i, 1000 + i))
    _prune_backups(tmp_path, keep_last=2)
    remaining = sorted(p.name for p in tmp_path.glob("*.sqlite"))
    assert remaining == ["arb-20260703.sqlite", "arb-20260704.sqlite"]  # 2 newest
