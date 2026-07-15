"""Read-only local dashboard (SPEC §14.3): a single HTML page reading
metrics.py output. GETs only — there is nothing to click and nothing
destructive to do. Client-disconnect tracebacks are swallowed with a one-line
log (forensics greps must stay clean)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from arb.logutil import get_logger

log = get_logger("arb.dashboard", "logs/scan.log")
PAGE = Path(__file__).parent / "dashboard.html"


def state(cfg, store) -> dict:
    from arb import report as rpt
    now = datetime.now(timezone.utc)
    day = now.astimezone(__import__("zoneinfo").ZoneInfo(str(cfg["timezone"]))) \
        .date().isoformat()
    from arb.metrics import day_bounds
    start, end = day_bounds(cfg, day)  # UTC bounds for the ET day (H5)
    frame = rpt.daily_frame(cfg, store, day)

    beats = [dict(r) for r in store.query(
        "SELECT process, MAX(beat_at) beat_at, head_sha FROM heartbeats GROUP BY process")]
    fresh = any((now - datetime.fromisoformat(b["beat_at"])).total_seconds()
                < int(cfg["heartbeat_stale_polls"]) * float(cfg["watch_interval_secs"])
                for b in beats)
    lock_pid = Path(".locks/supervisor/pid")
    lock_alive = False
    if lock_pid.exists():
        try:
            __import__("os").kill(int(lock_pid.read_text().strip()), 0)
            lock_alive = True
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    backups = sorted(Path(str(cfg["db_backup"]["dest"])).glob("*.sqlite"))
    backup_age_h = (now.timestamp() - backups[-1].stat().st_mtime) / 3600 \
        if backups else None

    open_bets = store.query(
        """SELECT fb.id, fb.kind, fb.event_key, fb.venue_pair, fb.edge_points,
                  fb.lane, fb.created_at,
                  EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id=fb.id
                          AND be.kind='confirmed') confirmed
           FROM flat_bets fb
           WHERE NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id=fb.id
                             AND be.kind IN ('settled','void','lifetime_closed'))
           ORDER BY fb.id DESC LIMIT 20""")
    consensus_at = store.query("SELECT MAX(computed_at) t FROM consensus")[0]["t"]
    consensus_today = store.query(
        "SELECT COUNT(*) n FROM consensus WHERE computed_at >= ? AND computed_at < ?",
        (start, end))[0]["n"]
    credits = store.query(
        "SELECT detail FROM annotations WHERE kind='oddsapi-credits' "
        "ORDER BY id DESC LIMIT 1")
    counters = store.query(
        """SELECT COALESCE(SUM(n_quotes),0) quotes, COUNT(*) polls
           FROM polls WHERE started_at >= ? AND started_at < ? AND kind='watch'""",
        (start, end))[0]
    events_today = store.query(
        """SELECT COUNT(DISTINCT event_key) n FROM quotes
           WHERE fetched_at >= ? AND fetched_at < ? AND event_key != ''""",
        (start, end))[0]["n"]
    opps_today = store.query(
        """SELECT o.kind, COUNT(*) n FROM opportunities o JOIN polls p ON p.id=o.poll_id
           WHERE p.started_at >= ? AND p.started_at < ? GROUP BY o.kind""",
        (start, end))
    rejects_today = store.query(
        """SELECT COUNT(*) n FROM left_on_table lo JOIN polls p ON p.id=lo.poll_id
           WHERE p.started_at >= ? AND p.started_at < ?""", (start, end))[0]["n"]
    venues = store.query(
        """SELECT pv.venue, AVG(pv.ok)*100 ok_pct, AVG(pv.latency_ms) latency,
                  SUM(pv.n_quotes) quotes, COUNT(*) polls
           FROM poll_venues pv JOIN polls p ON p.id = pv.poll_id
           WHERE p.started_at > ?
           GROUP BY pv.venue""",
        # Python ISO bound, never datetime('now'): the 'T' separator makes the
        # SQLite form compare by date only (the invariants.py trap)
        ((now - timedelta(hours=2)).isoformat(),))
    recent_flags = store.query(
        """SELECT o.kind, o.event_key, o.edge, o.status, o.notes, p.started_at
           FROM opportunities o JOIN polls p ON p.id = o.poll_id
           ORDER BY o.id DESC LIMIT 12""")

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "day": day,
        "supervisor": "ALIVE" if (fresh or lock_alive) else ("STALE" if beats else "DEAD"),
        "beats": beats,
        "head_sha": beats[0]["head_sha"] if beats else None,
        "data_age_secs": frame["data_age_secs"],
        "data_stale": frame["data_stale"],
        "backup_age_hours": backup_age_h,
        "stale_fees": frame["stale_fees"],
        "headline_pnl": str(frame["headline_pnl"]),
        "headline_n": frame["headline_n"],
        "cadence": frame["cadence"],
        "planned_interval": float(cfg["watch_interval_secs"]),
        "by_class": [{"kind": k, "lane": lane, **{kk: str(vv) for kk, vv in a.items()}}
                     for (k, lane), a in sorted(frame["by_class"].items())],
        "left_on_table": frame["left_on_table"],
        "capacity": {**frame["capacity"],
                     "projected_pnl": str(frame["capacity"]["projected_pnl"]),
                     "positions": [{k: str(v) for k, v in p.items()}
                                   for p in frame["capacity"]["positions"]]},
        "open_bets": [dict(r) for r in open_bets],
        "consensus_at": consensus_at,
        "consensus_rows_today": consensus_today,
        "odds_api_credits": int(credits[0]["detail"]) if credits else None,
        "counters": {"quotes": counters["quotes"], "polls": counters["polls"],
                     "events": events_today, "rejects": rejects_today,
                     "flags": {r["kind"]: r["n"] for r in opps_today}},
        "venues": [dict(r) for r in venues],
        "recent_flags": [dict(r) for r in recent_flags],
        "ev_enabled": bool(cfg["ev_enabled"]),
        "trusted_pairs": [str(p) for p in cfg["trusted_pairs"]],
    }


def make_handler(cfg, db_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            try:
                if self.path == "/api/state":
                    from arb.store import Store
                    body = json.dumps(state(cfg, Store(db_path)), default=str).encode()
                    ctype = "application/json"
                elif self.path == "/":
                    body = PAGE.read_bytes()
                    ctype = "text/html; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log.info("client disconnected mid-response")  # one line, no traceback
            except Exception as e:  # noqa: BLE001
                log.warning("dashboard error on %s: %s", self.path, e)
                try:
                    self.send_error(500)
                except Exception:  # noqa: BLE001
                    pass

        def log_message(self, fmt, *args):  # quiet: forensics greps stay clean
            pass

    return Handler


def serve(cfg, db_path: str) -> None:
    import os
    host = str(cfg["dashboard"]["host"])
    # PORT env overrides the config value: preview harnesses assign a free
    # port when the configured one is taken (host stays config-pinned —
    # never bound beyond localhost regardless)
    port = int(os.environ.get("PORT") or cfg["dashboard"]["port"])
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg, db_path))
    log.info("dashboard on http://%s:%d (read-only)", host, port)
    print(f"dashboard: http://{host}:{port}")
    httpd.serve_forever()
