"""Read-only local dashboard (SPEC §14.3): a single HTML page reading
metrics.py output. GETs only — there is nothing to click and nothing
destructive to do. Client-disconnect tracebacks are swallowed with a one-line
log (forensics greps must stay clean)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
    frame = rpt.daily_frame(cfg, store, day)

    beats = [dict(r) for r in store.query(
        "SELECT process, MAX(beat_at) beat_at, head_sha FROM heartbeats GROUP BY process")]
    fresh = any((now - datetime.fromisoformat(b["beat_at"])).total_seconds()
                < 3 * float(cfg["watch_interval_secs"]) for b in beats)
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
        """SELECT kind, COUNT(*) n FROM flat_bets fb
           WHERE NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id=fb.id
                             AND be.kind IN ('settled','void','lifetime_closed'))
           GROUP BY kind""")
    consensus_at = store.query("SELECT MAX(computed_at) t FROM consensus")[0]["t"]

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
        "open_bets": [dict(r) for r in open_bets],
        "consensus_at": consensus_at,
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
    host = str(cfg["dashboard"]["host"])
    port = int(cfg["dashboard"]["port"])
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg, db_path))
    log.info("dashboard on http://%s:%d (read-only)", host, port)
    print(f"dashboard: http://{host}:{port}")
    httpd.serve_forever()
