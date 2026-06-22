"""Read-only local web dashboard.

`python -m arb.experimental web` serves a live dashboard at http://localhost:8787 :
process health, bet pipeline, equity curves, segment effectiveness, recent bets, and
log tails. The page auto-refreshes by polling GET /api/state and only reads from the
WAL-backed SQLite database, so it is safe to run next to the daemon.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections import deque
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from arb.config import load_config
from arb.scoring import score_segments
from arb.store import Store

PROCESSES = [
    ("daemon", "arb daemon"),
    ("focus", "arb.experimental focus"),
    ("verify", "arb.experimental verify"),
    ("hot", "arb.experimental hot"),
    ("negrisk", "arb.experimental negrisk"),
    ("political", "arb.experimental political"),
    ("closing", "arb.experimental closing"),
]

LOG_FILES = {
    "daemon": Path("logs/daemon.log"),
    "focus": Path("logs/focus.log"),
    "verify": Path("logs/verify.log"),
    "hot": Path("logs/hot.log"),
    "negrisk": Path("logs/negrisk.log"),
    "political": Path("logs/political.log"),
    "closing": Path("logs/closing.log"),
}
CLV_CACHE_TTL_SECS = 300
_CLV_CACHE: dict[str, Any] = {"at": 0.0, "value": {"n": 0}}


def _process_health() -> list[dict[str, Any]]:
    try:
        ps = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        ps = ""
    out = []
    for name, pat in PROCESSES:
        count = sum(1 for line in ps.splitlines() if pat in line and "grep" not in line and "caffeinate" not in line)
        status = "ok" if count == 1 else ("down" if count == 0 else "duplicate")
        out.append({"name": name, "count": count, "status": status})
    return out


def _log_tail(path: Path, n: int = 90) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open(errors="replace") as handle:
            lines = deque((line.rstrip("\n") for line in handle if line.strip()), maxlen=n)
    except Exception:  # noqa: BLE001
        return []
    return list(lines)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(value: str | None) -> int | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


def _money(value: float | None) -> float:
    return round(float(value or 0.0), 2)


def _rows_by_key(rows, key: str) -> dict[str, dict[str, Any]]:
    return {row[key]: dict(row) for row in rows}


def _event_date_from_key(event_key: str | None) -> date | None:
    if not event_key:
        return None
    parts = event_key.split(":")
    if len(parts) < 2:
        return None
    try:
        return date.fromisoformat(parts[1])
    except ValueError:
        return None


def _horizon_bucket(event_date: date | None, now: datetime) -> tuple[str, str, int | None]:
    if event_date is None:
        return ("unknown", "No event date", None)
    days = (event_date - now.date()).days
    if days < 0:
        return ("overdue", "Past event date", days)
    if days == 0:
        return ("today", "Today", days)
    if days == 1:
        return ("tomorrow", "Tomorrow", days)
    if days <= 7:
        return ("week", "2-7 days", days)
    return ("later", "8+ days", days)


def _compact_event_name(event_key: str | None) -> str:
    if not event_key:
        return "unknown event"
    parts = event_key.split(":")
    if len(parts) >= 4:
        away = parts[2].replace("-", " ")
        home = parts[3].replace("-", " ")
        return f"{away} @ {home}"
    return event_key


def _open_bet_payload(row, now: datetime) -> dict[str, Any]:
    try:
        legs = json.loads(row["legs_json"])
    except (TypeError, json.JSONDecodeError):
        legs = []
    first = legs[0] if legs else {}
    event_key = first.get("event_key")
    event_date = _event_date_from_key(event_key)
    bucket, bucket_label, days_to_event = _horizon_bucket(event_date, now)
    age = _age_seconds(row["placed_at"])
    leg_labels = [
        f"{leg.get('outcome_label', '?')}@{leg.get('venue', '?')} {leg.get('cost', '?')}"
        for leg in legs[:4]
    ]
    if len(legs) > 4:
        leg_labels.append(f"+{len(legs) - 4} more")
    notes = [note for note in str(row["notes"] or "").split(",") if note]
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "status": row["status"],
        "edge": float(row["edge"] or 0),
        "sport": row["sport"],
        "category": row["category"],
        "quality": row["quality_flag"] or "clean",
        "trusted": bool(row["trusted"]),
        "live": bool(row["live"]),
        "placed_at": row["placed_at"],
        "age_seconds": age,
        "event_key": event_key,
        "event": _compact_event_name(event_key),
        "event_date": None if event_date is None else event_date.isoformat(),
        "days_to_event": days_to_event,
        "horizon": bucket,
        "horizon_label": bucket_label,
        "outcome": first.get("outcome_label"),
        "venue": first.get("venue"),
        "cost": first.get("cost"),
        "depth_usd": first.get("depth_usd"),
        "market_type": first.get("market_type"),
        "line": first.get("line"),
        "legs": leg_labels,
        "notes": notes[:5],
        "confirmed": bool(row["confirmed_n"]),
        "verified": int(row["verified_n"] or 0),
        "vanished": int(row["vanished_n"] or 0),
        "lifetime": int(row["lifetime_n"] or 0),
    }


def _cached_clv(store: Store) -> dict[str, Any]:
    now = time.monotonic()
    if now - float(_CLV_CACHE["at"]) < CLV_CACHE_TTL_SECS:
        return _CLV_CACHE["value"]
    try:
        from decimal import Decimal

        from arb.experimental.ev.clv import grade_clv

        value = grade_clv(store, Decimal("50"))
    except Exception:  # noqa: BLE001
        value = {"n": 0}
    _CLV_CACHE.update({"at": now, "value": value})
    return value


def build_state(store: Store, config) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with store.connect() as con:
        c = con.cursor()
        quotes = c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        polls = c.execute("SELECT COUNT(*) FROM polls").fetchone()[0]
        flat_bets = c.execute("SELECT COUNT(*) FROM flat_bets").fetchone()[0]
        max_poll_id = c.execute("SELECT COALESCE(MAX(id),0) FROM polls").fetchone()[0]
        max_flat_bet_id = c.execute("SELECT COALESCE(MAX(id),0) FROM flat_bets").fetchone()[0]
        open_bets = c.execute("SELECT COUNT(*) FROM flat_bets WHERE status IN ('OPEN','REVIEW','VERIFIED','CONFIRMED','LIFETIME')").fetchone()[0]
        last_poll = c.execute(
            "SELECT kind, started_at, finished_at, n_quotes, n_opps, credits_remaining FROM polls ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_quote = c.execute("SELECT MAX(fetched_at) FROM quotes").fetchone()[0]
        credits = c.execute(
            "SELECT credits_remaining FROM polls WHERE kind='scan' AND credits_remaining IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        arb_open = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(CAST(edge AS REAL)),0) e FROM flat_bets WHERE kind='ARB' AND status='OPEN'"
        ).fetchone()
        grades = _rows_by_key(
            c.execute("SELECT status, COUNT(*) n FROM flat_bets WHERE kind='ARB' GROUP BY status"),
            "status",
        )
        settled = _rows_by_key(
            c.execute(
                """
                SELECT result, COUNT(*) n, COALESCE(SUM(CAST(pnl_optimistic AS REAL)),0) pnl
                  FROM flat_bets
                 WHERE status='SETTLED' AND result IN ('WON','LOST','RESOLVED','VOID')
                 GROUP BY result
                """
            ),
            "result",
        )
        kind_rows = [
            dict(r)
            for r in c.execute(
                """
                SELECT kind,
                       COUNT(*) n,
                       SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) settled,
                       SUM(CASE WHEN status IN ('OPEN','REVIEW','VERIFIED','CONFIRMED','LIFETIME') THEN 1 ELSE 0 END) active,
                       COALESCE(SUM(CASE WHEN status='SETTLED' THEN CAST(pnl_optimistic AS REAL) ELSE 0 END),0) pnl,
                       AVG(CAST(edge AS REAL)) avg_edge,
                       SUM(CASE WHEN result='WON' THEN 1 ELSE 0 END) won,
                       SUM(CASE WHEN result='LOST' THEN 1 ELSE 0 END) lost
                  FROM flat_bets
                 WHERE kind != 'CLOSE'
                 GROUP BY kind
                 ORDER BY n DESC
                """
            )
        ]
        status_rows = [dict(r) for r in c.execute("SELECT kind, status, COUNT(*) n FROM flat_bets GROUP BY kind, status")]
        ev_suspect = c.execute("SELECT COUNT(*) FROM flat_bets WHERE kind='EV' AND quality_flag='suspect-edge'").fetchone()[0]
        quality_rows = [
            dict(r)
            for r in c.execute(
                """
                SELECT kind,
                       COALESCE(quality_flag,'clean') quality,
                       COUNT(*) n,
                       SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) settled,
                       SUM(CASE WHEN status IN ('OPEN','REVIEW','VERIFIED','CONFIRMED','LIFETIME') THEN 1 ELSE 0 END) active,
                       SUM(CASE WHEN result='WON' THEN 1 ELSE 0 END) won,
                       SUM(CASE WHEN result='LOST' THEN 1 ELSE 0 END) lost,
                       COALESCE(SUM(CASE WHEN status='SETTLED' THEN CAST(pnl_optimistic AS REAL) ELSE 0 END),0) pnl,
                       COALESCE(SUM(CASE WHEN status='SETTLED' THEN CAST(pnl_confirmed AS REAL) ELSE 0 END),0) confirmed_pnl,
                       AVG(CAST(edge AS REAL)) avg_edge
                  FROM flat_bets
                 WHERE kind != 'CLOSE'
                 GROUP BY kind, COALESCE(quality_flag,'clean')
                 ORDER BY kind, quality
                """
            )
        ]
        recent = [
            dict(r)
            for r in c.execute(
                """
                SELECT id, kind, status, edge, sport, category, placed_at, quality_flag, trusted, live, notes
                  FROM flat_bets
                 ORDER BY id DESC
                 LIMIT 35
                """
            )
        ]
        raw_open_rows = list(
            c.execute(
                """
                SELECT b.*,
                       (SELECT COUNT(*) FROM flat_bets r WHERE r.reappearance_of = b.id AND r.status = 'CONFIRMED') confirmed_n,
                       (SELECT COUNT(*) FROM flat_bets r WHERE r.reappearance_of = b.id AND r.status = 'VERIFIED') verified_n,
                       (SELECT COUNT(*) FROM flat_bets r WHERE r.reappearance_of = b.id AND r.status = 'VANISHED') vanished_n,
                       (SELECT COUNT(*) FROM flat_bets r WHERE r.reappearance_of = b.id AND r.status = 'LIFETIME') lifetime_n
                  FROM flat_bets b
                 WHERE b.status = 'OPEN'
                   AND b.kind IN ('ARB','EV','NEGRISK','DIVERGENCE')
                   AND NOT EXISTS (
                       SELECT 1 FROM flat_bets s
                        WHERE s.reappearance_of = b.id
                          AND s.status IN ('SETTLED','CLOSED')
                   )
                 ORDER BY b.id DESC
                 LIMIT 800
                """
            )
        )
        poll_series = [
            dict(r)
            for r in c.execute(
                """
                SELECT id, kind, started_at, finished_at, n_quotes, n_opps, credits_remaining
                  FROM polls
                 ORDER BY id DESC
                 LIMIT 40
                """
            )
        ][::-1]
        settled_rows = list(
            c.execute(
                """
                SELECT placed_at, pnl_optimistic, pnl_confirmed, quality_flag
                  FROM flat_bets
                 WHERE status='SETTLED' AND result IN ('WON','LOST','RESOLVED','VOID')
                 ORDER BY placed_at
                """
            )
        )

    def curve(field: str, clean: bool) -> list[dict[str, Any]]:
        cum, out = 0.0, []
        for row in settled_rows:
            if clean and row["quality_flag"] is not None:
                continue
            if row[field] is None:
                continue
            cum += float(row[field])
            out.append({"t": row["placed_at"], "y": round(cum, 4)})
        return out

    segments = [
        {
            "segment": score.segment,
            "n": score.n,
            "win_rate": score.win_rate,
            "roi": score.roi,
            "score": score.score,
            "tier": score.tier,
        }
        for score in score_segments(store, config.scoring)
    ]

    clv = _cached_clv(store)

    won_n, won_p = int(settled.get("WON", {}).get("n", 0)), float(settled.get("WON", {}).get("pnl", 0.0))
    lost_n, lost_p = int(settled.get("LOST", {}).get("n", 0)), float(settled.get("LOST", {}).get("pnl", 0.0))
    resolved_p = float(settled.get("RESOLVED", {}).get("pnl", 0.0))
    last_poll_started = last_poll["started_at"] if last_poll else None
    last_poll_finished = last_poll["finished_at"] if last_poll else None
    seen_fingerprints: set[str] = set()
    open_rows = []
    for row in raw_open_rows:
        fingerprint = row["fingerprint"]
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        open_rows.append(row)
        if len(open_rows) >= 250:
            break
    return {
        "now": now.isoformat(),
        "processes": _process_health(),
        "health": {
            "quotes": quotes,
            "polls": polls,
            "max_poll_id": int(max_poll_id),
            "flat_bets": flat_bets,
            "max_flat_bet_id": int(max_flat_bet_id),
            "open_bets": open_bets,
            "last_poll_kind": last_poll["kind"] if last_poll else None,
            "last_poll_at": last_poll_started,
            "last_poll_finished_at": last_poll_finished,
            "last_poll_age_seconds": _age_seconds(last_poll_finished or last_poll_started),
            "last_poll_quotes": last_poll["n_quotes"] if last_poll else None,
            "last_poll_opps": last_poll["n_opps"] if last_poll else None,
            "last_quote_at": last_quote,
            "last_quote_age_seconds": _age_seconds(last_quote),
            "credits": credits[0] if credits else None,
        },
        "arb": {
            "open_n": int(arb_open["n"]),
            "open_sum_edge": round(float(arb_open["e"]), 4),
            "verified": int(grades.get("VERIFIED", {}).get("n", 0)),
            "vanished": int(grades.get("VANISHED", {}).get("n", 0)),
            "unverifiable": int(grades.get("UNVERIFIABLE", {}).get("n", 0)),
            "settled_resolved_pnl": _money(resolved_p),
        },
        "grades": {status: int(row["n"]) for status, row in grades.items()},
        "settled": {"won_n": won_n, "lost_n": lost_n, "net": _money(won_p + lost_p + resolved_p)},
        "ev": {
            "total": next((int(row["n"]) for row in kind_rows if row["kind"] == "EV"), 0),
            "suspect": int(ev_suspect),
            "clv": clv,
        },
        "kinds": kind_rows,
        "quality": quality_rows,
        "statuses": status_rows,
        "equity": {
            "optimistic_raw": curve("pnl_optimistic", False),
            "optimistic_clean": curve("pnl_optimistic", True),
            "confirmed_clean": curve("pnl_confirmed", True),
        },
        "segments": segments,
        "open_bets": [_open_bet_payload(row, now) for row in open_rows],
        "polls": poll_series,
        "recent": recent,
        "logs": {name: _log_tail(path) for name, path in LOG_FILES.items()},
    }


class Handler(BaseHTTPRequestHandler):
    store: Store
    config = None

    def log_message(self, *args):  # noqa: ANN002
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/state"):
            body = json.dumps(build_state(self.store, self.config)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(port: int = 8787) -> None:
    config = load_config()
    Handler.store = Store(config.database_path)
    Handler.config = config
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard: http://localhost:{port}  (read-only; Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ArbitrageModel Live</title>
<style>
:root{
  --bg:#101113;--panel:#18191d;--panel-2:#202126;--line:#303238;--text:#f2f3f0;--muted:#a4a6ad;
  --green:#56c271;--red:#ff6b63;--gold:#e0ad3b;--blue:#6aa8ff;--violet:#b58cff;--cyan:#5ac8c8;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button{font:inherit}
.top{position:sticky;top:0;z-index:5;background:rgba(16,17,19,.96);border-bottom:1px solid var(--line)}
.top-inner{display:flex;align-items:center;justify-content:space-between;gap:18px;max-width:1440px;margin:0 auto;padding:14px 20px}
.brand{display:flex;align-items:baseline;gap:12px;min-width:260px}
h1{font-size:18px;line-height:1;margin:0;font-weight:720;letter-spacing:0}
.sub{color:var(--muted);font-size:13px}
.procs{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}
.proc{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--panel);height:28px;padding:0 9px;border-radius:8px;color:var(--muted);font-size:12px}
.dot{width:8px;height:8px;border-radius:99px;background:var(--muted)}
.ok .dot{background:var(--green);box-shadow:0 0 10px rgba(86,194,113,.6)}
.down .dot{background:var(--red)}
.duplicate .dot{background:var(--gold)}
.wrap{max-width:1440px;margin:0 auto;padding:18px 20px 28px}
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;min-width:0}
.metric{grid-column:span 2;padding:14px}
.wide{grid-column:span 8}.side{grid-column:span 4}.half{grid-column:span 6}.full{grid-column:span 12}
.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:7px}
.value{font-size:27px;line-height:1.05;font-weight:760;letter-spacing:0;white-space:nowrap}
.unit{font-size:12px;color:var(--muted);font-weight:520;margin-left:5px}
.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--gold)}.blue{color:var(--blue)}.muted{color:var(--muted)}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 14px;border-bottom:1px solid var(--line)}
.panel-title{font-size:13px;font-weight:700;letter-spacing:0}
.panel-body{padding:14px}
.chart{width:100%;height:272px;display:block}
.mini{height:136px}
.bars{display:grid;gap:9px}
.bar-row{display:grid;grid-template-columns:92px 1fr 42px;align-items:center;gap:10px;font-size:12px}
.bar-track{height:9px;background:#111216;border:1px solid var(--line);border-radius:99px;overflow:hidden}
.bar-fill{height:100%;background:var(--blue)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:8px 7px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:650}
tr:last-child td{border-bottom:0}
.badge{display:inline-flex;align-items:center;height:22px;padding:0 7px;border-radius:6px;font-size:11px;font-weight:740;background:#25262c;color:var(--muted)}
.badge.OPEN,.badge.REVIEW{color:var(--blue);background:rgba(106,168,255,.13)}
.badge.VERIFIED,.badge.CONFIRMED,.badge.WON{color:var(--green);background:rgba(86,194,113,.14)}
.badge.VANISHED,.badge.LOST,.badge.BLOCKED{color:var(--red);background:rgba(255,107,99,.14)}
.badge.LIFETIME,.badge.SETTLED,.badge.RESOLVED{color:var(--violet);background:rgba(181,140,255,.13)}
.badge.UNVERIFIABLE,.badge.INSUFFICIENT_DATA{color:var(--gold);background:rgba(224,173,59,.13)}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{height:28px;border-radius:7px;border:1px solid var(--line);background:var(--panel-2);color:var(--muted);padding:0 9px;cursor:pointer}
.tab.active{color:var(--text);border-color:#555963;background:#2a2c33}
.log{margin:0;height:322px;overflow:auto;background:#0b0c0e;border:1px solid var(--line);border-radius:8px;padding:12px;color:#d2d5d0;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap}
.status-line{display:flex;align-items:center;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px}
.pill{display:inline-flex;align-items:center;height:24px;padding:0 8px;border:1px solid var(--line);border-radius:99px;background:var(--panel-2);color:var(--muted)}
.action{height:30px;border-radius:7px;border:1px solid var(--line);background:var(--panel-2);color:var(--text);padding:0 10px;cursor:pointer}
.action:hover{border-color:#555963;background:#2a2c33}
.track-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
.track-card{background:#111216;border:1px solid var(--line);border-radius:8px;padding:11px 12px;min-width:0}
.track-card .label{margin-bottom:5px}
.track-card .value{font-size:21px}
.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.detail-block{min-width:0}
.detail-title{font-size:12px;font-weight:700;margin:0 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.delta-table td:nth-child(n+2),.delta-table th:nth-child(n+2){text-align:right}
.open-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.open-groups{display:grid;gap:14px}
.open-group-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 7px;color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.open-table td{font-size:12px}
.open-event{font-weight:700;color:var(--text);max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.subline{color:var(--muted);font-size:11px;margin-top:2px;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (max-width:1100px){.metric{grid-column:span 4}.wide,.side,.half{grid-column:span 12}.top-inner{align-items:flex-start;flex-direction:column}.procs{justify-content:flex-start}}
@media (max-width:900px){.track-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.detail-grid{grid-template-columns:1fr}}
@media (max-width:700px){.metric{grid-column:span 6}.wrap{padding:12px}.top-inner{padding:12px}.value{font-size:22px}.chart{height:230px}.log{height:260px}.track-grid{grid-template-columns:repeat(2,minmax(0,1fr))}th:nth-child(5),td:nth-child(5){display:none}}
</style>
</head>
<body>
<header class="top">
  <div class="top-inner">
    <div class="brand">
      <h1>ArbitrageModel</h1>
      <div class="sub" id="clock">connecting</div>
    </div>
    <div class="procs" id="procs"></div>
  </div>
</header>
<main class="wrap">
  <section class="grid" id="metrics"></section>
  <section class="grid" style="margin-top:12px">
    <div class="panel full">
      <div class="panel-head">
        <div>
          <div class="panel-title">Tracking Run</div>
          <div class="muted" id="track-start"></div>
        </div>
        <div class="status-line">
          <span class="pill" id="track-mode">local baseline</span>
          <button class="action" id="reset-track" type="button">Reset</button>
        </div>
      </div>
      <div class="panel-body">
        <div class="track-grid" id="track-cards"></div>
        <div id="track-detail" style="margin-top:12px"></div>
      </div>
    </div>
    <div class="panel full">
      <div class="panel-head open-toolbar">
        <div>
          <div class="panel-title">Open Bets &amp; EV</div>
          <div class="muted" id="open-summary"></div>
        </div>
        <div class="tabs" id="open-tabs"></div>
      </div>
      <div class="panel-body"><div class="open-groups" id="open-bets"></div></div>
    </div>
    <div class="panel wide">
      <div class="panel-head">
        <div class="panel-title">Settled P&L</div>
        <div class="status-line"><span class="pill">raw</span><span class="pill">clean</span><span class="pill">confirmed</span></div>
      </div>
      <div class="panel-body"><svg class="chart" id="equity"></svg></div>
    </div>
    <div class="panel side">
      <div class="panel-head"><div class="panel-title">Bet Pipeline</div><div class="muted" id="pipeline-total"></div></div>
      <div class="panel-body"><div class="bars" id="pipeline"></div></div>
    </div>
    <div class="panel half">
      <div class="panel-head"><div class="panel-title">Poll Throughput</div><div class="muted" id="poll-summary"></div></div>
      <div class="panel-body"><svg class="chart mini" id="polls"></svg></div>
    </div>
    <div class="panel half">
      <div class="panel-head"><div class="panel-title">Strategy Effectiveness</div><div class="muted" id="clv"></div></div>
      <div class="panel-body" id="kinds"></div>
    </div>
    <div class="panel half">
      <div class="panel-head"><div class="panel-title">Segments</div><div class="muted">clean settled rows</div></div>
      <div class="panel-body" id="segments"></div>
    </div>
    <div class="panel half">
      <div class="panel-head"><div class="panel-title">Recent Bets</div><div class="muted">append-only ledger</div></div>
      <div class="panel-body" id="recent"></div>
    </div>
    <div class="panel full">
      <div class="panel-head">
        <div class="panel-title">Live Output</div>
        <div class="tabs" id="log-tabs"></div>
      </div>
      <div class="panel-body"><pre class="log" id="log"></pre></div>
    </div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let state = null;
let activeLog = "daemon";
let activeOpenKind = "EV";
const colors = {raw:"#a4a6ad", clean:"#6aa8ff", confirmed:"#56c271", opps:"#e0ad3b"};
const trackKey = "arb-dashboard-tracking-v1";
const esc = v => String(v ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const num = v => Number(v || 0);
const compact = v => new Intl.NumberFormat(undefined,{notation:"compact",maximumFractionDigits:1}).format(num(v));
const money = v => `${num(v) >= 0 ? "+$" : "-$"}${Math.abs(num(v)).toFixed(2)}`;
const pct = v => `${(num(v) * 100).toFixed(1)}%`;
const age = seconds => {
  if (seconds == null) return "no data";
  if (seconds < 90) return `${seconds}s ago`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
};
const duration = seconds => {
  if (seconds < 90) return `${seconds}s`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `${hours}h ${rem}m` : `${hours}h`;
};
const valClass = v => num(v) >= 0 ? "good" : "bad";

function indexRows(rows, keyFn) {
  return Object.fromEntries((rows || []).map(row => [keyFn(row), row]));
}

function trackingSnapshot(d) {
  return {
    at: d.now,
    health: {
      quotes: num(d.health.quotes),
      max_poll_id: num(d.health.max_poll_id),
      max_flat_bet_id: num(d.health.max_flat_bet_id),
      open_bets: num(d.health.open_bets),
    },
    settled: {...d.settled},
    arb: {...d.arb},
    grades: {...d.grades},
    kinds: indexRows(d.kinds, row => row.kind),
    quality: indexRows(d.quality, row => `${row.kind}:${row.quality}`),
  };
}

function loadBaseline(d) {
  let baseline = null;
  try { baseline = JSON.parse(localStorage.getItem(trackKey) || "null"); } catch (_) { baseline = null; }
  if (!baseline || !baseline.at || !baseline.health) {
    baseline = trackingSnapshot(d);
    localStorage.setItem(trackKey, JSON.stringify(baseline));
  }
  return baseline;
}

function resetTracking(d) {
  if (!d) return;
  localStorage.setItem(trackKey, JSON.stringify(trackingSnapshot(d)));
  render(d);
}

function delta(current, base, field) {
  return num(current?.[field]) - num(base?.[field]);
}

function cleanAvgEdge(snapshot, kind, fallbackRow) {
  const clean = snapshot.quality?.[`${kind}:clean`];
  return clean?.avg_edge ?? fallbackRow?.avg_edge;
}

function deltaCard(label, value, detail, cls="") {
  return `<div class="track-card"><div class="label">${esc(label)}</div><div class="value ${cls}">${value}</div><div class="muted">${detail}</div></div>`;
}

function metric(label, value, detail, cls="") {
  return `<div class="panel metric"><div class="label">${esc(label)}</div><div class="value ${cls}">${value}</div><div class="muted" style="margin-top:8px">${detail}</div></div>`;
}

function drawLines(svg, series, opts={}) {
  const w = svg.clientWidth || 800, h = svg.clientHeight || 260, pad = opts.pad ?? 18;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const points = series.flatMap(s => s.data || []);
  svg.innerHTML = "";
  if (!points.length) {
    svg.innerHTML = `<text x="${w/2}" y="${h/2}" fill="#a4a6ad" text-anchor="middle">No settled points yet</text>`;
    return;
  }
  const xs = points.map(p => Date.parse(p.t)).filter(Number.isFinite);
  const ys = points.map(p => num(p.y));
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  let minY = Math.min(...ys), maxY = Math.max(...ys);
  if (minY === maxY) { minY -= 1; maxY += 1; }
  const x = t => pad + ((Date.parse(t) - minX) / Math.max(1, maxX - minX)) * (w - pad * 2);
  const y = v => h - pad - ((num(v) - minY) / (maxY - minY)) * (h - pad * 2);
  const zero = y(0);
  svg.insertAdjacentHTML("beforeend", `<line x1="${pad}" x2="${w-pad}" y1="${zero}" y2="${zero}" stroke="#303238" stroke-width="1"/>`);
  series.forEach(s => {
    const d = (s.data || []).map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join(" ");
    if (d) svg.insertAdjacentHTML("beforeend", `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>`);
  });
  const latest = series.map(s => {
    const last = (s.data || [])[s.data.length - 1];
    return last ? `<span style="color:${s.color}">${esc(s.label)} ${money(last.y)}</span>` : "";
  }).filter(Boolean).join(" &nbsp; ");
  svg.insertAdjacentHTML("beforeend", `<foreignObject x="${pad}" y="4" width="${w-pad*2}" height="26"><div xmlns="http://www.w3.org/1999/xhtml" style="font:12px system-ui;color:#a4a6ad">${latest}</div></foreignObject>`);
}

function drawBars(svg, rows) {
  const w = svg.clientWidth || 600, h = svg.clientHeight || 136, pad = 12;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.innerHTML = "";
  if (!rows.length) return;
  const max = Math.max(...rows.map(r => num(r.n_quotes)), 1);
  const bw = (w - pad * 2) / rows.length;
  rows.forEach((r, i) => {
    const bh = (num(r.n_quotes) / max) * (h - pad * 2);
    const x = pad + i * bw;
    svg.insertAdjacentHTML("beforeend", `<rect x="${x.toFixed(1)}" y="${(h-pad-bh).toFixed(1)}" width="${Math.max(2,bw-2).toFixed(1)}" height="${bh.toFixed(1)}" fill="#6aa8ff" opacity=".78"/>`);
    if (num(r.n_opps) > 0) {
      const oh = Math.max(3, (num(r.n_opps) / Math.max(...rows.map(p => num(p.n_opps)), 1)) * 42);
      svg.insertAdjacentHTML("beforeend", `<rect x="${x.toFixed(1)}" y="${(h-pad-oh).toFixed(1)}" width="${Math.max(2,bw-2).toFixed(1)}" height="${oh.toFixed(1)}" fill="#e0ad3b"/>`);
    }
  });
}

function table(headers, rows, empty="No rows yet") {
  if (!rows.length) return `<div class="muted">${empty}</div>`;
  return `<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

function shortAge(seconds) {
  if (seconds == null) return "unknown";
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function horizonText(bet) {
  if (bet.days_to_event == null) return bet.horizon_label;
  if (bet.days_to_event < 0) return `${Math.abs(bet.days_to_event)}d past`;
  if (bet.days_to_event === 0) return "today";
  if (bet.days_to_event === 1) return "tomorrow";
  return `${bet.days_to_event}d`;
}

function renderMetrics(d) {
  const running = d.processes.filter(p => p.status === "ok").length;
  $("metrics").innerHTML = [
    metric("Realized P&L", `<span class="${valClass(d.settled.net)}">${money(d.settled.net)}</span>`, `${d.settled.won_n} won / ${d.settled.lost_n} lost`),
    metric("Open Bets", compact(d.health.open_bets), `${compact(d.health.flat_bets)} ledger rows`, "blue"),
    metric("ARB Verification", `<span class="good">${d.arb.verified}</span><span class="muted"> / </span><span class="bad">${d.arb.vanished}</span>`, `${d.arb.unverifiable} unverifiable`),
    metric("ARB Locked P&L", `<span class="${valClass(d.arb.settled_resolved_pnl)}">${money(d.arb.settled_resolved_pnl)}</span>`, `${d.arb.open_n} open, ${pct(d.arb.open_sum_edge)} edge`),
    metric("Quotes", compact(d.health.quotes), `latest ${age(d.health.last_quote_age_seconds)}`),
    metric("Processes", `${running}/${d.processes.length}`, `poll ${age(d.health.last_poll_age_seconds)}`, running ? "good" : "bad"),
  ].join("");
}

function renderProcesses(d) {
  $("clock").textContent = `live ${new Date(d.now).toLocaleTimeString()} | ${compact(d.health.polls)} polls`;
  $("procs").innerHTML = d.processes.map(p => `<span class="proc ${p.status}"><span class="dot"></span>${esc(p.name)}${p.status === "ok" ? "" : ` ${esc(p.status)}`}</span>`).join("");
}

function renderPipeline(d) {
  const max = Math.max(...Object.values(d.grades).map(num), 1);
  $("pipeline-total").textContent = `${d.arb.open_n} open ARBs`;
  const order = ["OPEN","VERIFIED","CONFIRMED","LIFETIME","SETTLED","VANISHED","UNVERIFIABLE","REVIEW"];
  $("pipeline").innerHTML = order.filter(k => d.grades[k]).map(k => {
    const n = d.grades[k], color = k === "VANISHED" ? "var(--red)" : k === "UNVERIFIABLE" ? "var(--gold)" : k === "VERIFIED" || k === "CONFIRMED" ? "var(--green)" : "var(--blue)";
    return `<div class="bar-row"><span><span class="badge ${k}">${k}</span></span><span class="bar-track"><span class="bar-fill" style="width:${Math.max(4,n/max*100)}%;background:${color}"></span></span><b>${n}</b></div>`;
  }).join("") || `<div class="muted">No ARB rows yet</div>`;
}

function renderOpenBets(d) {
  const counts = (d.open_bets || []).reduce((acc, bet) => {
    acc.ALL = (acc.ALL || 0) + 1;
    acc[bet.kind] = (acc[bet.kind] || 0) + 1;
    return acc;
  }, {});
  const kinds = ["EV", "ARB", "NEGRISK", "DIVERGENCE", "ALL"].filter(kind => counts[kind]);
  if (!kinds.includes(activeOpenKind)) activeOpenKind = kinds.includes("EV") ? "EV" : (kinds[0] || "ALL");
  $("open-tabs").innerHTML = kinds.map(kind => `<button class="tab ${kind === activeOpenKind ? "active" : ""}" data-kind="${esc(kind)}">${esc(kind)} ${counts[kind]}</button>`).join("");
  $("open-tabs").querySelectorAll("button").forEach(btn => btn.onclick = () => { activeOpenKind = btn.dataset.kind; renderOpenBets(state); });
  const shown = (d.open_bets || []).filter(bet => activeOpenKind === "ALL" || bet.kind === activeOpenKind);
  const clean = shown.filter(bet => bet.quality === "clean").length;
  $("open-summary").textContent = `${shown.length} shown · ${clean} clean · grouped by event date`;
  const order = ["overdue", "today", "tomorrow", "week", "later", "unknown"];
  const labels = {overdue:"Past event date", today:"Today", tomorrow:"Tomorrow", week:"2-7 days", later:"8+ days", unknown:"No event date"};
  const groups = order
    .map(key => [key, shown.filter(bet => bet.horizon === key)])
    .filter(([, bets]) => bets.length);
  if (!groups.length) {
    $("open-bets").innerHTML = `<div class="muted">No open ${esc(activeOpenKind)} bets right now.</div>`;
    return;
  }
  $("open-bets").innerHTML = groups.map(([key, bets]) => {
    const rows = bets.slice(0, 60).map(bet => {
      const detail = [
        bet.outcome ? `${bet.outcome}` : "",
        bet.venue ? `@${bet.venue}` : "",
        bet.cost ? `cost ${bet.cost}` : "",
        bet.depth_usd ? `depth $${bet.depth_usd}` : "",
      ].filter(Boolean).join(" ");
      const flags = [
        bet.quality !== "clean" ? `<span class="badge UNVERIFIABLE">${esc(bet.quality)}</span>` : "",
        bet.confirmed ? `<span class="badge CONFIRMED">CONF</span>` : "",
        bet.verified ? `<span class="badge VERIFIED">V${bet.verified}</span>` : "",
        bet.vanished ? `<span class="badge VANISHED">X${bet.vanished}</span>` : "",
        bet.live ? `<span class="badge REVIEW">LIVE</span>` : "",
      ].filter(Boolean).join(" ");
      return `<tr>
        <td class="muted mono">${bet.id}</td>
        <td><span class="badge ${esc(bet.kind)}">${esc(bet.kind)}</span></td>
        <td><div class="open-event" title="${esc(bet.event_key || bet.event)}">${esc(bet.event)}</div><div class="subline">${esc(detail || bet.legs.join(", "))}</div></td>
        <td>${esc(bet.event_date || "unknown")}<div class="subline">${esc(horizonText(bet))}</div></td>
        <td class="${valClass(bet.edge)}">${pct(bet.edge)}</td>
        <td><span class="badge ${esc(bet.status)}">${esc(bet.status)}</span><div class="subline">${flags}</div></td>
        <td class="muted">${shortAge(bet.age_seconds)}</td>
      </tr>`;
    });
    const hidden = bets.length > 60 ? `<div class="muted" style="margin-top:8px">Showing latest 60 of ${bets.length} in this bucket.</div>` : "";
    return `<div>
      <div class="open-group-title"><span>${labels[key]}</span><span>${bets.length}</span></div>
      ${table(["ID","Kind","Event / leg","Event date","Edge","Status","Age"], rows).replace("<table>", "<table class=\"open-table\">")}
      ${hidden}
    </div>`;
  }).join("");
}

function renderTracking(d) {
  const base = loadBaseline(d);
  const current = trackingSnapshot(d);
  const started = new Date(base.at);
  const elapsedSeconds = Math.max(0, Math.round((Date.parse(d.now) - Date.parse(base.at)) / 1000));
  const evCleanNow = current.quality["EV:clean"] || {};
  const evCleanBase = base.quality["EV:clean"] || {};
  const evSuspectNow = current.quality["EV:suspect-edge"] || {};
  const evSuspectBase = base.quality["EV:suspect-edge"] || {};
  const cleanRows = Object.keys(current.quality)
    .filter(key => key.endsWith(":clean"))
    .reduce((sum, key) => sum + delta(current.quality[key], base.quality[key], "n"), 0);
  const cleanPnl = Object.keys(current.quality)
    .filter(key => key.endsWith(":clean"))
    .reduce((sum, key) => sum + delta(current.quality[key], base.quality[key], "pnl"), 0);
  const cleanConfirmed = Object.keys(current.quality)
    .filter(key => key.endsWith(":clean"))
    .reduce((sum, key) => sum + delta(current.quality[key], base.quality[key], "confirmed_pnl"), 0);
  $("track-start").textContent = `started ${started.toLocaleString()} · elapsed ${duration(elapsedSeconds)}`;
  $("track-cards").innerHTML = [
    deltaCard("New polls", `+${delta(current.health, base.health, "max_poll_id")}`, `${compact(delta(current.health, base.health, "quotes"))} quotes`),
    deltaCard("New ledger rows", `+${delta(current.health, base.health, "max_flat_bet_id")}`, `${cleanRows >= 0 ? "+" : ""}${cleanRows} clean rows`),
    deltaCard("Clean paper P&L", money(cleanPnl), "optimistic settled", valClass(cleanPnl)),
    deltaCard("Confirmed P&L", money(cleanConfirmed), "settled rows with recheck", valClass(cleanConfirmed)),
    deltaCard("Clean EV", `+${delta(evCleanNow, evCleanBase, "n")}`, `${money(delta(evCleanNow, evCleanBase, "pnl"))} paper`, valClass(delta(evCleanNow, evCleanBase, "pnl"))),
    deltaCard("ARB verify", `+${delta(current.grades, base.grades, "VERIFIED")} / +${delta(current.grades, base.grades, "VANISHED")}`, "verified / vanished"),
  ].join("");
  const kindNames = Array.from(new Set([...Object.keys(base.kinds), ...Object.keys(current.kinds)])).filter(k => k !== "CLOSE").sort();
  const kindRows = kindNames.map(kind => {
    const now = current.kinds[kind] || {}, then = base.kinds[kind] || {};
    const pnl = delta(now, then, "pnl");
    const edge = cleanAvgEdge(current, kind, now);
    return `<tr><td><b>${esc(kind)}</b></td><td>${delta(now, then, "n") >= 0 ? "+" : ""}${delta(now, then, "n")}</td><td>${delta(now, then, "active") >= 0 ? "+" : ""}${delta(now, then, "active")}</td><td>${delta(now, then, "settled") >= 0 ? "+" : ""}${delta(now, then, "settled")}</td><td class="${valClass(pnl)}">${money(pnl)}</td><td>${edge == null ? "" : pct(edge)}</td></tr>`;
  });
  const evQualityRows = [
    ["EV clean", evCleanNow, evCleanBase],
    ["EV suspect", evSuspectNow, evSuspectBase],
  ].map(([label, now, then]) => {
    const pnl = delta(now, then, "pnl"), conf = delta(now, then, "confirmed_pnl");
    return `<tr><td><b>${esc(label)}</b></td><td>${delta(now, then, "n") >= 0 ? "+" : ""}${delta(now, then, "n")}</td><td>${delta(now, then, "settled") >= 0 ? "+" : ""}${delta(now, then, "settled")}</td><td>${delta(now, then, "won") >= 0 ? "+" : ""}${delta(now, then, "won")}</td><td>${delta(now, then, "lost") >= 0 ? "+" : ""}${delta(now, then, "lost")}</td><td class="${valClass(pnl)}">${money(pnl)}</td><td class="${valClass(conf)}">${money(conf)}</td></tr>`;
  });
  $("track-detail").innerHTML = `
    <div class="detail-grid">
      <div class="detail-block"><div class="detail-title">Since Baseline By Kind</div>${table(["Kind","Rows","Active","Settled","P&L","Clean Edge"], kindRows).replace("<table>", "<table class=\"delta-table\">")}</div>
      <div class="detail-block"><div class="detail-title">EV Quality Split</div>${table(["Bucket","Rows","Settled","Won","Lost","Paper","Confirmed"], evQualityRows).replace("<table>", "<table class=\"delta-table\">")}</div>
    </div>`;
}

function renderKinds(d) {
  const rows = d.kinds.map(k => {
    const pnl = num(k.pnl);
    const edge = (d.quality || []).find(q => q.kind === k.kind && q.quality === "clean")?.avg_edge ?? k.avg_edge;
    return `<tr><td><b>${esc(k.kind)}</b></td><td>${k.n}</td><td>${k.active}</td><td>${k.settled}</td><td class="${valClass(pnl)}">${money(pnl)}</td><td>${edge == null ? "" : pct(edge)}</td></tr>`;
  });
  $("kinds").innerHTML = table(["Kind","Rows","Active","Settled","P&L","Clean Edge"], rows);
  const clv = d.ev.clv || {};
  $("clv").textContent = clv.n ? `CLV n=${clv.n}, mean ${num(clv.mean_clv).toFixed(4)}` : "CLV gathering";
}

function renderSegments(d) {
  const rows = d.segments.slice(0, 12).map(s => `<tr><td>${esc(s.segment)}</td><td>${s.n}</td><td>${pct(s.win_rate)}</td><td class="${valClass(s.roi)}">${pct(s.roi)}</td><td>${s.score}</td><td><span class="badge ${esc(s.tier)}">${esc(s.tier)}</span></td></tr>`);
  $("segments").innerHTML = table(["Segment","N","Win","ROI","Score","Tier"], rows);
}

function renderRecent(d) {
  const rows = d.recent.map(r => `<tr><td class="muted">${r.id}</td><td>${esc(r.kind)}</td><td><span class="badge ${esc(r.status)}">${esc(r.status)}</span></td><td>${pct(r.edge)}</td><td>${esc(r.sport || r.category || "")}</td><td class="muted">${age(Math.round((Date.now()-Date.parse(r.placed_at))/1000))}</td></tr>`);
  $("recent").innerHTML = table(["ID","Kind","Status","Edge","Segment","Age"], rows);
}

function renderLogs(d) {
  const names = Object.keys(d.logs);
  if (!names.includes(activeLog)) activeLog = names[0] || "daemon";
  $("log-tabs").innerHTML = names.map(name => `<button class="tab ${name === activeLog ? "active" : ""}" data-log="${esc(name)}">${esc(name)}</button>`).join("");
  $("log-tabs").querySelectorAll("button").forEach(btn => btn.onclick = () => { activeLog = btn.dataset.log; renderLogs(state); });
  const log = $("log");
  const atBottom = log.scrollHeight - log.clientHeight - log.scrollTop < 32;
  log.textContent = (d.logs[activeLog] || []).join("\n") || `${activeLog}.log has no output yet`;
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function render(d) {
  renderProcesses(d);
  renderMetrics(d);
  renderTracking(d);
  renderOpenBets(d);
  renderPipeline(d);
  renderKinds(d);
  renderSegments(d);
  renderRecent(d);
  renderLogs(d);
  $("poll-summary").textContent = `${d.health.last_poll_quotes ?? 0} quotes, ${d.health.last_poll_opps ?? 0} opps`;
  drawLines($("equity"), [
    {label:"raw", color:colors.raw, data:d.equity.optimistic_raw},
    {label:"clean", color:colors.clean, data:d.equity.optimistic_clean},
    {label:"confirmed", color:colors.confirmed, data:d.equity.confirmed_clean},
  ]);
  drawBars($("polls"), d.polls);
}

$("reset-track").onclick = () => resetTracking(state);

async function tick() {
  try {
    const response = await fetch("/api/state", {cache:"no-store"});
    state = await response.json();
    render(state);
  } catch (err) {
    $("clock").textContent = "disconnected";
  }
}
tick();
setInterval(tick, 5000);
window.addEventListener("resize", () => state && render(state));
</script>
</body>
</html>"""
