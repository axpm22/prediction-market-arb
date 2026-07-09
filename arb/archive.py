"""arb archive (H8, SPEC §13.3): move quotes older than archive_after_days into
monthly archive DBs, then delete them from the hot DB and VACUUM. Quotes are the
bulk of DB growth (~3 GB/day, most of it ladder_json). Rule 7 requires PERSISTING
every quote (it is the backtest data) — not keeping them all in the hot DB
forever. Quotes are NOT flat_bets/bet_events, so rule 9 doesn't protect them; the
hot-DB delete only ever touches `quotes`, so the CI grep stays green.

Safety: every archived row is verified present in the archive (inserted count ==
source count) BEFORE the delete commits, in one transaction per month (attach +
INSERT..SELECT + verify + DELETE). A partial/failed month rolls back whole."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from arb.logutil import get_logger

log = get_logger("arb.archive", "logs/scan.log")

_PRED = "strftime('%Y%m', fetched_at) = ? AND fetched_at < ?"


def run_archive(cfg, store, archive_dir="data/archive", now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=int(cfg["archive_after_days"]))).isoformat()
    conn = store.conn
    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT strftime('%Y%m', fetched_at) FROM quotes WHERE fetched_at < ?",
        (cutoff,)).fetchall()]
    if not months:
        return {"moved": 0, "months": []}
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    prev = conn.isolation_level
    conn.isolation_level = None  # explicit transaction control (ATTACH/VACUUM can't nest in a txn)
    moved, done = 0, []
    try:
        for month in months:
            adb = archive_dir / f"arb-{month}.sqlite"
            conn.execute("ATTACH DATABASE ? AS arch", (str(adb),))
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS arch.quotes "
                             "AS SELECT * FROM main.quotes WHERE 0")
                conn.execute("BEGIN IMMEDIATE")
                n_src = conn.execute(
                    f"SELECT COUNT(*) FROM main.quotes WHERE {_PRED}",
                    (month, cutoff)).fetchone()[0]
                cur = conn.execute(
                    f"INSERT INTO arch.quotes SELECT * FROM main.quotes WHERE {_PRED}",
                    (month, cutoff))
                if cur.rowcount != n_src:  # archive incomplete -> keep the hot rows
                    conn.execute("ROLLBACK")
                    raise RuntimeError(
                        f"archive verify failed for {month}: archived "
                        f"{cur.rowcount} of {n_src}")
                conn.execute(f"DELETE FROM main.quotes WHERE {_PRED}", (month, cutoff))
                conn.execute("COMMIT")
                moved += n_src
                done.append(month)
            finally:
                conn.execute("DETACH DATABASE arch")
        conn.execute("VACUUM")
    finally:
        conn.isolation_level = prev
    log.info("archived %s quotes into %s monthly archive(s)", moved, len(done))
    return {"moved": moved, "months": done}
