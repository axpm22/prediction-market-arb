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


def _tail_errors(limit: int = 30) -> list[dict]:
    """Last WARNING/ERROR lines from today's live logs, newest first. Reads
    only the final 64 KiB of each file (scan.log runs to MBs). Honors
    ARB_LOG_DIR like logutil so tests never touch production logs."""
    import os
    root = Path(os.environ.get("ARB_LOG_DIR") or ".")
    out = []
    for name in ("supervisor.log", "scan.log", "regrade.log", "unmatched.log"):
        p = root / "logs" / name
        if not p.exists():
            continue
        with p.open("rb") as f:
            f.seek(max(0, p.stat().st_size - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for ln in lines:
            parts = ln.split(" ", 4)  # date time LEVEL logger msg
            if len(parts) == 5 and parts[2] in ("WARNING", "ERROR", "CRITICAL"):
                try:
                    # asctime is LOCAL time; emit aware UTC ISO for the client
                    at = datetime.strptime(
                        f"{parts[0]} {parts[1].split(',')[0]}",
                        "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc).isoformat()
                except ValueError:
                    continue
                out.append({"at": at, "level": parts[2], "source": parts[3],
                            "msg": parts[4][:300], "file": name})
    out.sort(key=lambda e: e["at"], reverse=True)
    return out[:limit]


def _lane_timing(cfg, store, day_start: str) -> list[dict]:
    """Per-lane cadence: configured interval vs last poll actually persisted."""
    from arb.heartbeat import lane_interval
    last = {r["kind"]: dict(r) for r in store.query(
        """SELECT kind, MAX(started_at) last_poll, COUNT(*) polls_today,
                  COALESCE(SUM(n_quotes),0) quotes_today
           FROM polls WHERE started_at >= ? GROUP BY kind""", (day_start,))}
    lanes = [k for k, v in cfg["supervisor"]["processes"].items() if v]
    lanes += [k for k in last if k not in lanes]
    return [{"name": k, "interval_secs": lane_interval(cfg, k),
             "last_poll": (last.get(k) or {}).get("last_poll"),
             "polls_today": (last.get(k) or {}).get("polls_today", 0),
             "quotes_today": (last.get(k) or {}).get("quotes_today", 0)}
            for k in lanes]


def _daily_schedule(cfg, now) -> list[dict]:
    """Supervisor-owned daily slots with seconds until next firing."""
    from zoneinfo import ZoneInfo

    from arb.heartbeat import _schedule
    local = now.astimezone(ZoneInfo(str(cfg["timezone"])))
    out = []
    for key, hhmm in _schedule(cfg):
        h, m = map(int, hhmm.split(":"))
        slot = local.replace(hour=h, minute=m, second=0, microsecond=0)
        if slot <= local:
            slot += timedelta(days=1)
        out.append({"key": key, "at": hhmm,
                    "due_in_secs": int((slot - local).total_seconds())})
    return sorted(out, key=lambda s: s["due_in_secs"])


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

    from arb.dashboard_data import OPEN_WHERE     # one "open position" predicate
    open_bets = store.query(
        f"""SELECT fb.id, fb.kind, fb.event_key, fb.venue_pair, fb.edge_points,
                  fb.lane, fb.created_at, fb.stake_usd, fb.status,
                  EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id=fb.id
                          AND be.kind='confirmed') confirmed
           FROM flat_bets fb WHERE {OPEN_WHERE}
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
        "lanes": _lane_timing(cfg, store, start),
        "errors": _tail_errors(),
        "schedule": _daily_schedule(cfg, now),
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
        "by_class": [{"kind": k, "lane": lane, "variant": variant,
                      **{kk: str(vv) for kk, vv in a.items()}}
                     for (k, lane, variant), a in sorted(frame["by_class"].items())],
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
        "refresh_secs": int(cfg["dashboard"]["refresh_secs"]),
        "pnl_days": int(cfg["dashboard"]["pnl_days"]),
    }


def _section(cfg, store, path: str, query: dict) -> dict:
    """One JSON section (Task 12). A section that raises returns its own error
    instead of 500-ing the page: a broken whale tape must not take the P&L
    curves down with it.

    The two whale sections are the expensive ones (a frame over ~200k tape
    prints); they go through dashboard_cache's TTL memo (dashboard.cache_secs)
    so concurrent pollers share one computation instead of each starting
    their own (live 2026-09-04: that pileup outran the page's own refresh).
    `/api/whale/traders?wallet=` folds the wallet into the cache key, so the
    memo is LRU-capped at dashboard.cache_max_entries (review of 9e5951a) —
    otherwise every distinct wallet ever queried adds a permanent entry."""
    from arb import dashboard_cache, dashboard_whale
    from arb import dashboard_data as dd
    ttl = float(cfg["dashboard"]["cache_secs"])
    max_entries = int(cfg["dashboard"]["cache_max_entries"])
    try:
        if path == "/api/state":
            return state(cfg, store)
        if path == "/api/pnl":
            days = int(query.get("days", [cfg["dashboard"]["pnl_days"]])[0])
            return dd.pnl_series(cfg, store, max(1, min(days, 365)))
        if path == "/api/positions":
            return {"positions": dd.positions(cfg, store)}
        if path == "/api/lanes":
            return {"lanes": dd.lane_verdicts(cfg, store)}
        if path == "/api/whale/traders":
            level = query.get("level", ["bucket"])[0]
            top = query.get("top", [None])[0]
            min_n = query.get("min_n", [None])[0]
            wallet = query.get("wallet", [None])[0]
            key = ("whale_traders", level, top, min_n, wallet)
            return dashboard_cache.get_or_compute(
                key, ttl, lambda: dashboard_whale.whale_traders(
                    cfg, level=level, top=top, min_n=min_n, wallet=wallet),
                max_entries=max_entries)
        return {"whale": dashboard_cache.get_or_compute(
            ("whale_panel",), ttl, lambda: dashboard_whale.whale_panel(cfg, store),
            max_entries=max_entries)}
    except Exception as e:  # noqa: BLE001 — see docstring
        log.warning("dashboard section %s failed: %s", path, e, exc_info=True)
        return {"error": f"{type(e).__name__}: {e}"}


def make_handler(cfg, db_path):
    from arb import dashboard_data as dd
    sections = ("/api/state", "/api/pnl", "/api/positions", "/api/lanes",
                "/api/whale", "/api/whale/traders")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            try:
                from urllib.parse import parse_qs, urlparse
                url = urlparse(self.path)
                if url.path in sections:
                    from arb.store import Store
                    # close per request: an unclosed reader lingering on GC's
                    # schedule pins the WAL and starves auto-checkpoint
                    st = Store(db_path)
                    try:
                        body = json.dumps(dd.jsonable(_section(
                            cfg, st, url.path, parse_qs(url.query))),
                            default=str).encode()
                    finally:
                        st.close()
                    ctype = "application/json"
                elif self.path == "/":
                    # {{REFRESH_SECS}} is the page's bootstrap refresh interval,
                    # before /api/state has a chance to name its own
                    body = PAGE.read_bytes().replace(
                        b"{{REFRESH_SECS}}",
                        str(int(cfg["dashboard"]["refresh_secs"])).encode())
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
