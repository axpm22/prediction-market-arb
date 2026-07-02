"""Read-only local web dashboard.

`python -m arb.experimental web` serves a live dashboard at http://localhost:8787 :
process health, bet pipeline, equity curves, segment effectiveness, recent bets, and
log tails. The page auto-refreshes by polling GET /api/state and only reads from the
WAL-backed SQLite database, so it is safe to run next to the daemon.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import deque
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from arb.arb_sizing import build_arb_scale_projection, build_arb_sizing_model, recommend_arb_stake
from arb.config import load_config, overlay_path
from arb.experimental import control
from arb.scoring import score_segments
from arb.store import Store

PROCESSES = [
    ("daemon", "arb daemon"),
    ("ev-exit", "arb.experimental ev-exit"),
    ("negrisk", "arb.experimental negrisk"),
    ("political", "arb.experimental political"),
    ("nonsports", "arb.experimental nonsports"),
    ("closing", "arb.experimental closing"),
    ("btc", "arb.experimental btc"),
]

LOG_FILES = {
    "daemon": Path("logs/daemon.log"),
    "focus": Path("logs/focus.log"),
    "verify": Path("logs/verify.log"),
    "hot": Path("logs/hot.log"),
    "ev-exit": Path("logs/ev-exit.log"),
    "negrisk": Path("logs/negrisk.log"),
    "political": Path("logs/political.log"),
    "closing": Path("logs/closing.log"),
    "nonsports": Path("logs/nonsports.log"),
    "btc": Path("logs/btc.log"),
    "whales": Path("logs/whales.log"),
}

EDGE_SIZED_EV_BASELINES = {
    "keep": {
        "label": "2026-06-23 KEEP-only edge-sized start",
        "max_id": 7924,
        "settled": 35,
        "stake": 22.20,
        "pnl": 8.01,
        "filter_label": "validation_tag=KEEP",
    },
    "broad": {
        "label": "2026-06-23 all-clean edge-sized start",
        "max_id": 7924,
        "settled": 487,
        "stake": 681.18,
        "pnl": 113.42,
        "filter_label": "no validation filter",
    },
}

SCANNERS = [
    {
        "id": "daemon",
        "name": "Sports Watch",
        "process": "daemon",
        "stop": "daemon",
        "command": [sys.executable, "-m", "arb", "daemon"],
        "log": LOG_FILES["daemon"],
        "poll_kinds": ["watch", "scan"],
        "next_where": "category='sports' AND kind IN ('EV','ARB')",
        "return_where": "category='sports' AND kind IN ('EV','ARB')",
        "detail": "MLB now; also stores configured soccer, tennis, MMA quotes.",
    },
    {
        "id": "micro_ev",
        "name": "Micro EV",
        "process": "daemon",
        "stop": "daemon",
        "command": None,
        "log": LOG_FILES["daemon"],
        "poll_kinds": ["watch"],
        "next_where": "kind='EV' AND quality_flag='micro'",
        "return_where": "kind='EV' AND quality_flag='micro'",
        "detail": "Thin-book EV only; $5-$50 visible depth, stricter edge, depth-capped stake.",
        "control": "parent",
    },
    {
        "id": "ev_exit",
        "name": "EV Exit",
        "process": "ev-exit",
        "stop": "ev-exit",
        "command": [sys.executable, "-m", "arb.experimental", "ev-exit"],
        "log": LOG_FILES["ev-exit"],
        "poll_kinds": ["ev-exit"],
        "next_where": "kind='EV' AND (quality_flag IS NULL OR quality_flag='micro')",
        "return_where": "kind='EV' AND status='EXITED'",
        "detail": "Paper take-profit exits; executable sell bid after fees, only when remaining edge is mostly gone.",
    },
    {
        "id": "negrisk",
        "name": "Negative Risk",
        "process": "negrisk",
        "stop": "negrisk",
        "command": [sys.executable, "-m", "arb.experimental", "negrisk"],
        "log": LOG_FILES["negrisk"],
        "poll_kinds": ["negrisk"],
        "next_where": "kind='NEGRISK'",
        "return_where": "kind='NEGRISK'",
        "detail": "Single-venue basket arbs; only clear if capturable depth exists.",
    },
    {
        "id": "political",
        "name": "Politics / Econ / Weather",
        "process": "political",
        "stop": "political",
        "command": [sys.executable, "-m", "arb.experimental", "political"],
        "log": LOG_FILES["political"],
        "poll_kinds": ["political"],
        "next_where": "(sport='political' OR category IN ('politics','economics','climate'))",
        "return_where": "(sport='political' OR category IN ('politics','economics','climate'))",
        "detail": "Kalshi intra-market YES+NO only; no cross-venue wording risk.",
    },
    {
        "id": "nonsports",
        "name": "Mapped Weather / Macro",
        "process": "nonsports",
        "stop": "nonsports",
        "command": [sys.executable, "-m", "arb.experimental", "nonsports"],
        "log": LOG_FILES["nonsports"],
        "poll_kinds": ["nonsports"],
        "next_where": "category='nonsports' AND kind NOT IN ('NEGRISK','ARB_BTC')",
        "return_where": "category='nonsports' AND kind NOT IN ('NEGRISK','ARB_BTC')",
        "detail": "Only manually reviewed weather/macro maps; every flag stays REVIEW.",
    },
    {
        "id": "closing",
        "name": "Closing-Line EV",
        "process": "closing",
        "stop": "closing",
        "command": [sys.executable, "-m", "arb.experimental", "closing"],
        "log": LOG_FILES["closing"],
        "poll_kinds": ["closing"],
        "next_where": "kind='EV' AND notes LIKE '%closing-line%'",
        "return_where": "kind='EV' AND notes LIKE '%closing-line%'",
        "detail": "One late snapshot per event; currently scoped to MLB.",
    },
    {
        "id": "btc",
        "name": "BTC / Crypto",
        "process": "btc",
        "stop": "btc",
        "command": [sys.executable, "-m", "arb.experimental", "btc"],
        "log": LOG_FILES["btc"],
        "poll_kinds": ["btc"],
        "next_where": "(sport='btc_15m' OR category='btc_15m' OR kind='ARB_BTC')",
        "return_where": "(sport='btc_15m' OR category='btc_15m' OR kind='ARB_BTC')",
        "detail": "Cross-venue crypto windows; REVIEW until oracle equivalence is proven.",
    },
]
SCANNERS_BY_ID = {scanner["id"]: scanner for scanner in SCANNERS}
CLV_CACHE_TTL_SECS = 300
_CLV_CACHE: dict[str, Any] = {"at": 0.0, "value": {"n": 0}}


_PS_CACHE: dict[str, Any] = {"at": 0.0, "value": []}
_PS_TTL_SECS = 4.0


def _stop_path(name: str) -> Path:
    return Path("data") / f"stop-{name}"


def _stop_requested(name: str) -> bool:
    return Path("data/stop-all").exists() or _stop_path(name).exists()


def _process_count(pattern: str, ps: str) -> int:
    return sum(1 for line in ps.splitlines() if pattern in line and "grep" not in line and "caffeinate" not in line)


def _scanner_processes(ps: str | None = None) -> dict[str, dict[str, Any]]:
    if ps is None:
        try:
            ps = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001
            ps = ""
    out = {}
    process_patterns = {name: pattern for name, pattern in PROCESSES}
    for scanner in SCANNERS:
        process = str(scanner["process"])
        pattern = process_patterns.get(process, f"arb.experimental {process}")
        count = _process_count(pattern, ps)
        stopped = _stop_requested(str(scanner["stop"]))
        if stopped and count > 0:
            status = "stopping"
        elif stopped:
            status = "disabled"
        elif count == 1:
            status = "ok"
        elif count == 0:
            status = "down"
        else:
            status = "duplicate"
        out[scanner["id"]] = {
            "id": scanner["id"],
            "name": process,
            "label": scanner["name"],
            "count": count,
            "status": status,
            "stopped": stopped,
            "control": scanner.get("control", "direct"),
        }
    return out


def _process_health() -> list[dict[str, Any]]:
    # Cache the ps spawn briefly — the dashboard polls every 8s (× open tabs) and process
    # status barely changes between polls; no need to fork ps on every request.
    nowm = time.monotonic()
    if nowm - float(_PS_CACHE["at"]) < _PS_TTL_SECS and _PS_CACHE["value"]:
        return _PS_CACHE["value"]
    out = [
        row
        for scanner_id, row in _scanner_processes().items()
        if SCANNERS_BY_ID.get(scanner_id, {}).get("control") != "parent"
    ]
    _PS_CACHE.update({"at": nowm, "value": out})
    return out


def _log_tail(path: Path, n: int = 90, tail_bytes: int = 65536) -> list[str]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            # Seek from the end so cost is O(tail_bytes), not O(filesize) — logs grow
            # unbounded over a multi-day run and this is read every dashboard refresh.
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
                handle.readline()  # drop the partial first line
            text = handle.read().decode(errors="replace")
        lines = deque((ln for ln in text.splitlines() if ln.strip()), maxlen=n)
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
        return ("overdue", "Awaiting settlement", days)
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


def _open_bet_payload(row, now: datetime, arb_sizing_model: dict[str, Any] | None = None) -> dict[str, Any]:
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
    quality = row["quality_flag"] or "clean"
    lane = "MICRO_EV" if row["kind"] == "EV" and quality == "micro" else row["kind"]
    payload = {
        "id": int(row["id"]),
        "kind": row["kind"],
        "lane": lane,
        "display_kind": "MICRO EV" if lane == "MICRO_EV" else row["kind"],
        "status": row["status"],
        "edge": float(row["edge"] or 0),
        "sport": row["sport"],
        "category": row["category"],
        "quality": quality,
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
        "stake_usd": row["stake_usd"],
        "market_type": first.get("market_type"),
        "line": first.get("line"),
        "legs": leg_labels,
        "notes": notes[:5],
        "confirmed": bool(row["confirmed_n"]),
        "verified": int(row["verified_n"] or 0),
        "vanished": int(row["vanished_n"] or 0),
        "lifetime": int(row["lifetime_n"] or 0),
    }
    if row["kind"] == "ARB" and arb_sizing_model is not None:
        payload["arb_sizing"] = recommend_arb_stake(legs, row["edge_points"], arb_sizing_model)
    return payload


def _leg_start(leg: dict[str, Any], event_key: str | None) -> datetime | None:
    raw = leg.get("start_utc")
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    event_date = _event_date_from_key(event_key)
    if event_date is None:
        return None
    return datetime(event_date.year, event_date.month, event_date.day, tzinfo=timezone.utc)


def _scanner_next_event(con, where: str, now: datetime) -> dict[str, Any] | None:
    rows = list(
        con.execute(
            f"""
            SELECT id, kind, status, edge, legs_json, placed_at, notes
              FROM flat_bets b
             WHERE b.status IN ('OPEN','CONFIRMED')
               AND {where}
               AND NOT EXISTS (
                   SELECT 1 FROM flat_bets s
                    WHERE s.status IN ('SETTLED','CLOSED','EXITED')
                      AND s.reappearance_of = CASE WHEN b.status='CONFIRMED' THEN b.reappearance_of ELSE b.id END
               )
             ORDER BY b.id DESC
             LIMIT 800
            """
        )
    )
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    fallback: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        try:
            legs = json.loads(row["legs_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            legs = []
        first = legs[0] if legs else {}
        event_key = first.get("event_key")
        start = _leg_start(first, event_key)
        if start is None:
            continue
        item = {
            "id": int(row["id"]),
            "kind": row["kind"],
            "status": row["status"],
            "event": _compact_event_name(event_key),
            "event_key": event_key,
            "start": start.isoformat(),
            "starts_in_seconds": int((start - now).total_seconds()),
            "edge": round(float(row["edge"] or 0), 4),
        }
        if start >= now:
            candidates.append((start, item))
        else:
            fallback.append((start, item))
    if candidates:
        return min(candidates, key=lambda x: x[0])[1]
    if fallback:
        return max(fallback, key=lambda x: x[0])[1]
    return None


def _scanner_returns(con, where: str) -> dict[str, Any]:
    row = con.execute(
        f"""
        SELECT COUNT(*) n,
               COALESCE(SUM(CAST(pnl_optimistic AS REAL)),0) pnl,
               COALESCE(SUM(CAST(stake_usd AS REAL)),0) stake,
               AVG(CAST(pnl_optimistic AS REAL)) avg_pnl
          FROM flat_bets
         WHERE status IN ('SETTLED','EXITED')
           AND result IN ('WON','LOST','RESOLVED','VOID','SOLD')
           AND {where}
        """
    ).fetchone()
    n = int(row["n"] or 0)
    pnl = float(row["pnl"] or 0)
    stake = float(row["stake"] or 0)
    return {
        "settled": n,
        "pnl": _money(pnl),
        "stake": _money(stake),
        "roi": round(pnl / stake, 4) if stake else None,
        "avg_pnl": _money(row["avg_pnl"]) if n else None,
    }


def _scanner_last_poll(con, poll_kinds: list[str]) -> dict[str, Any] | None:
    if not poll_kinds:
        return None
    placeholders = ",".join("?" for _ in poll_kinds)
    row = con.execute(
        f"""
        SELECT kind, started_at, finished_at, n_quotes, n_opps
          FROM polls
         WHERE kind IN ({placeholders})
         ORDER BY id DESC
         LIMIT 1
        """,
        poll_kinds,
    ).fetchone()
    if row is None:
        return None
    at = row["finished_at"] or row["started_at"]
    return {
        "kind": row["kind"],
        "at": at,
        "age_seconds": _age_seconds(at),
        "quotes": row["n_quotes"],
        "opps": row["n_opps"],
    }


def _scanner_summaries(con, processes: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    by_id = {row["id"]: row for row in processes}
    out = []
    for scanner in SCANNERS:
        scanner_id = str(scanner["id"])
        proc = by_id.get(scanner_id) or by_id.get(str(scanner["process"]), {})
        returns = _scanner_returns(con, str(scanner["return_where"]))
        out.append(
            {
                "id": scanner_id,
                "name": scanner["name"],
                "status": proc.get("status", "down"),
                "count": proc.get("count", 0),
                "stopped": bool(proc.get("stopped")),
                "control": scanner.get("control", "direct"),
                "detail": scanner["detail"],
                "next": _scanner_next_event(con, str(scanner["next_where"]), now),
                "returns": returns,
                "last_poll": _scanner_last_poll(con, list(scanner["poll_kinds"])),
            }
        )
    return out


def _negrisk_capturable(notes: str | None) -> float:
    """Parse the 'capturable=$X' figure the negrisk sweep writes into notes."""
    for token in str(notes or "").replace("|", " ").split():
        if token.startswith("capturable=$"):
            try:
                return float(token[len("capturable=$"):])
            except ValueError:
                return 0.0
    return 0.0


def _negrisk_executable(row) -> bool:
    """A NEGRISK basket is executable only with real legs, a P&L value, and positive
    capturable depth. Current sweep rows have empty legs / $0 capturable -> not executable."""
    legs_json = row["legs_json"] or "[]"
    try:
        has_legs = bool(json.loads(legs_json))
    except (TypeError, json.JSONDecodeError):
        has_legs = False
    return has_legs and row["pnl_optimistic"] is not None and _negrisk_capturable(row["notes"]) > 0


def _cached_clv(store: Store, config) -> dict[str, Any]:
    now = time.monotonic()
    if now - float(_CLV_CACHE["at"]) < CLV_CACHE_TTL_SECS:
        return _CLV_CACHE["value"]
    try:
        # K13: the cockpit shows the DEFAULT no-vig gate metric, not the fee-loaded one
        # documented "BIASED — comparison only"; depth floor from config, not a literal.
        from arb.experimental.ev.clv import clv_result

        value = clv_result(store, fee_loaded=False, min_depth=config.min_executable_usd)
    except Exception:  # noqa: BLE001
        value = {"n": 0}
    _CLV_CACHE.update({"at": now, "value": value})
    return value


def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _launch_scanner(scanner: dict[str, Any]) -> dict[str, Any]:
    if not scanner.get("command"):
        return {"ok": False, "error": f"{scanner['name']} runs inside {scanner['process']} and is not a separate process"}
    process_info = _scanner_processes().get(str(scanner["id"]), {})
    if int(process_info.get("count") or 0) > 0:
        return {"ok": True, "message": "already running"}
    Path("logs").mkdir(exist_ok=True)
    log_path = Path(scanner["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as out:
        subprocess.Popen(  # noqa: S603 - fixed local command list from SCANNERS.
            list(scanner["command"]),
            cwd=Path.cwd(),
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _PS_CACHE.update({"at": 0.0, "value": []})
    return {"ok": True, "message": "started"}


def _set_scanner_state(scanner_id: str, action: str) -> dict[str, Any]:
    scanner = SCANNERS_BY_ID.get(scanner_id)
    if scanner is None:
        return {"ok": False, "error": f"unknown scanner {scanner_id!r}"}
    if action not in {"start", "stop"}:
        return {"ok": False, "error": "action must be start or stop"}
    if scanner.get("control") == "parent":
        return {"ok": False, "error": f"{scanner['name']} is a lane inside {SCANNERS_BY_ID[str(scanner['process'])]['name']}; start/stop that worker instead"}
    Path("data").mkdir(exist_ok=True)
    stop_path = _stop_path(str(scanner["stop"]))
    if action == "stop":
        stop_path.write_text(datetime.now(timezone.utc).isoformat())
        _PS_CACHE.update({"at": 0.0, "value": []})
        return {"ok": True, "message": "stop requested"}
    if stop_path.exists():
        stop_path.unlink()
    return _launch_scanner(scanner)


def build_state(store: Store, config) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with store.connect() as con:
        c = con.cursor()
        quotes = c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        polls = c.execute("SELECT COUNT(*) FROM polls").fetchone()[0]
        flat_bets = c.execute("SELECT COUNT(*) FROM flat_bets").fetchone()[0]
        max_poll_id = c.execute("SELECT COALESCE(MAX(id),0) FROM polls").fetchone()[0]
        max_flat_bet_id = c.execute("SELECT COALESCE(MAX(id),0) FROM flat_bets").fetchone()[0]
        open_bets = c.execute(
            """
            SELECT COUNT(*)
              FROM flat_bets b
             WHERE b.kind != 'CLOSE'
               AND b.status IN ('OPEN','REVIEW','VERIFIED','CONFIRMED','LIFETIME')
               AND NOT EXISTS (
                   SELECT 1 FROM flat_bets s
                    WHERE s.reappearance_of = b.id
                      AND s.status IN ('SETTLED','CLOSED','EXITED')
               )
            """
        ).fetchone()[0]
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
                 WHERE status IN ('SETTLED','EXITED') AND result IN ('WON','LOST','RESOLVED','VOID','SOLD')
                 GROUP BY result
                """
            ),
            "result",
        )
        # EV is judged ALONE: clean, realized EV rows only. SOLD is a paper take-profit
        # exit, not a game outcome, but it is fillable P&L at the executable bid.
        clean_ev = c.execute(
            """
            SELECT COUNT(*) settled,
                   SUM(CASE WHEN result='WON' THEN 1 ELSE 0 END) won,
                   SUM(CASE WHEN result='LOST' THEN 1 ELSE 0 END) lost,
                   SUM(CASE WHEN result='SOLD' THEN 1 ELSE 0 END) sold,
                   COALESCE(SUM(CAST(pnl_optimistic AS REAL)),0) pnl,
                   COALESCE(SUM(CAST(stake_usd AS REAL)),0) stake
             FROM flat_bets
             WHERE kind='EV' AND status IN ('SETTLED','EXITED')
               AND result IN ('WON','LOST','VOID','SOLD') AND quality_flag IS NULL
               AND (validation_tag IS NULL OR validation_tag='KEEP')
            """
        ).fetchone()
        # Quarantined EV rows shown on their own line, never in Clean EV P&L.
        suspect_ev = c.execute(
            """
            SELECT COUNT(*) n, COALESCE(SUM(CAST(pnl_optimistic AS REAL)),0) pnl
             FROM flat_bets
             WHERE kind='EV' AND status IN ('SETTLED','EXITED')
               AND result IN ('WON','LOST','VOID','SOLD') AND quality_flag='suspect-edge'
            """
        ).fetchone()
        rejected_ev = c.execute(
            """
            SELECT COUNT(*) n
              FROM flat_bets
             WHERE kind='EV'
               AND status='CLOSED'
               AND quality_flag IN (
                   'validation-drop',
                   'in-play-skipped',
                   'missing-start-skipped',
                   'terminal-fingerprint-skipped',
                   'duplicate-position-skipped',
                   'manual-resolver-required',
                   'invalid-in-play'
               )
            """
        ).fetchone()
        # Micro EV is practical-but-thin: separate from clean EV so small-depth rows do
        # not inflate the headline model while still showing whether tiny sizing works.
        micro_ev = c.execute(
            """
            SELECT COUNT(*) settled,
                   SUM(CASE WHEN result='WON' THEN 1 ELSE 0 END) won,
                   SUM(CASE WHEN result='LOST' THEN 1 ELSE 0 END) lost,
                   SUM(CASE WHEN result='SOLD' THEN 1 ELSE 0 END) sold,
                   COALESCE(SUM(CAST(pnl_optimistic AS REAL)),0) pnl,
                   COALESCE(SUM(CAST(stake_usd AS REAL)),0) stake
              FROM flat_bets
             WHERE kind='EV' AND status IN ('SETTLED','EXITED')
               AND result IN ('WON','LOST','VOID','SOLD') AND quality_flag='micro'
            """
        ).fetchone()
        micro_active = c.execute(
            """
            SELECT COUNT(*) n, COALESCE(SUM(CAST(stake_usd AS REAL)),0) stake
              FROM flat_bets
             WHERE kind='EV' AND quality_flag='micro'
               AND status IN ('OPEN','REVIEW','VERIFIED','CONFIRMED','LIFETIME')
               AND NOT EXISTS (
                   SELECT 1 FROM flat_bets s
                    WHERE s.reappearance_of = flat_bets.id
                      AND s.status IN ('SETTLED','CLOSED','EXITED')
               )
            """
        ).fetchone()
        def edge_sized_since(baseline: dict[str, Any], keep_only: bool) -> Any:
            validation_filter = "AND validation_tag='KEEP'" if keep_only else ""
            return c.execute(
                f"""
                SELECT COUNT(*) settled,
                       SUM(CASE WHEN result='WON' THEN 1 ELSE 0 END) won,
                       SUM(CASE WHEN result='LOST' THEN 1 ELSE 0 END) lost,
                       COALESCE(SUM(CAST(edge AS REAL) * 100.0),0) stake,
                       COALESCE(SUM(
                           CASE WHEN CAST(stake_usd AS REAL) > 0
                                THEN CAST(pnl_optimistic AS REAL) * (CAST(edge AS REAL) * 100.0) / CAST(stake_usd AS REAL)
                                ELSE 0 END
                       ),0) pnl,
                       COALESCE(SUM(
                           CASE WHEN pnl_confirmed IS NOT NULL AND CAST(stake_usd AS REAL) > 0
                                THEN CAST(pnl_confirmed AS REAL) * (CAST(edge AS REAL) * 100.0) / CAST(stake_usd AS REAL)
                                ELSE 0 END
                       ),0) confirmed_pnl,
                       COALESCE(AVG(CAST(edge AS REAL) * 100.0),0) avg_stake,
                       COALESCE(MAX(CAST(edge AS REAL) * 100.0),0) max_stake,
                       COALESCE(MAX(id), ?) max_id
                  FROM flat_bets
                 WHERE id > ?
                   AND kind='EV'
                   AND status IN ('SETTLED','EXITED')
                   AND result IN ('WON','LOST')
                   AND quality_flag IS NULL
                   {validation_filter}
                """,
                (baseline["max_id"], baseline["max_id"]),
            ).fetchone()

        edge_sized_rows = {
            "keep": edge_sized_since(EDGE_SIZED_EV_BASELINES["keep"], keep_only=True),
            "broad": edge_sized_since(EDGE_SIZED_EV_BASELINES["broad"], keep_only=False),
        }
        # ARB gets its own lane: locked/provisional, includes RESOLVED. Never in EV totals.
        # Split trusted (whitelisted pair → clean lane) from REVIEW (trusted=0, hard rule #6:
        # provisional, never real money) so the UX shows the A1 gate, not one blended number.
        locked_arb = c.execute(
            """
            SELECT COUNT(*) settled,
                   SUM(CASE WHEN result='RESOLVED' THEN 1 ELSE 0 END) resolved,
                   COALESCE(SUM(CAST(pnl_optimistic AS REAL)),0) pnl,
                   COALESCE(SUM(CAST(stake_usd AS REAL)),0) stake,
                   SUM(CASE WHEN trusted=1 THEN 1 ELSE 0 END) trusted_settled,
                   COALESCE(SUM(CASE WHEN trusted=1 THEN CAST(pnl_optimistic AS REAL) ELSE 0 END),0) trusted_pnl,
                   SUM(CASE WHEN trusted=0 THEN 1 ELSE 0 END) review_settled,
                   COALESCE(SUM(CASE WHEN trusted=0 THEN CAST(pnl_optimistic AS REAL) ELSE 0 END),0) review_pnl,
                   SUM(CASE WHEN quality_flag='invalid-in-play' THEN 1 ELSE 0 END) invalid_in_play
              FROM flat_bets
             WHERE kind='ARB' AND status='SETTLED'
            """
        ).fetchone()
        # INVALID rows are CLOSED, not SETTLED — count the A3 timing-guard rejections separately.
        invalid_arb = c.execute(
            "SELECT COUNT(*) n FROM flat_bets WHERE kind='ARB' AND status='CLOSED' AND quality_flag='invalid-in-play'"
        ).fetchone()
        # DIVERGENCE is a research spread signal — never settled, never P&L.
        research_open = c.execute(
            "SELECT COUNT(*) n FROM flat_bets WHERE kind='DIVERGENCE' AND status='OPEN'"
        ).fetchone()[0]
        kind_rows = [
            dict(r)
            for r in c.execute(
                """
                SELECT kind,
                       COUNT(*) n,
                       SUM(CASE WHEN status IN ('SETTLED','EXITED') THEN 1 ELSE 0 END) settled,
                       SUM(CASE WHEN status IN ('OPEN','REVIEW','VERIFIED','CONFIRMED','LIFETIME')
                                  AND NOT EXISTS (
                                      SELECT 1 FROM flat_bets s
                                       WHERE s.reappearance_of = flat_bets.id
                                         AND s.status IN ('SETTLED','CLOSED','EXITED')
                                  )
                                THEN 1 ELSE 0 END) active,
                       COALESCE(SUM(CASE WHEN status IN ('SETTLED','EXITED') THEN CAST(pnl_optimistic AS REAL) ELSE 0 END),0) pnl,
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
                       SUM(CASE WHEN status IN ('SETTLED','EXITED') THEN 1 ELSE 0 END) settled,
                       SUM(CASE WHEN status IN ('OPEN','REVIEW','VERIFIED','CONFIRMED','LIFETIME')
                                  AND NOT EXISTS (
                                      SELECT 1 FROM flat_bets s
                                       WHERE s.reappearance_of = flat_bets.id
                                         AND s.status IN ('SETTLED','CLOSED','EXITED')
                                  )
                                THEN 1 ELSE 0 END) active,
                       SUM(CASE WHEN result='WON' THEN 1 ELSE 0 END) won,
                       SUM(CASE WHEN result='LOST' THEN 1 ELSE 0 END) lost,
                       COALESCE(SUM(CASE WHEN status IN ('SETTLED','EXITED') THEN CAST(pnl_optimistic AS REAL) ELSE 0 END),0) pnl,
                       COALESCE(SUM(CASE WHEN status IN ('SETTLED','EXITED') THEN CAST(pnl_confirmed AS REAL) ELSE 0 END),0) confirmed_pnl,
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
                   AND b.kind IN ('ARB','EV','NEGRISK')
                   AND NOT EXISTS (
                       SELECT 1 FROM flat_bets s
                        WHERE s.reappearance_of = b.id
                          AND s.status IN ('SETTLED','CLOSED','EXITED')
                   )
                 ORDER BY b.id DESC
                 LIMIT 800
                """
            )
        )
        arb_sizing_model = build_arb_sizing_model(c, config.bankroll_usd, config.max_stake_pct,
                                                  cfg=config.arb_sizing)
        arb_scale_projection = build_arb_scale_projection(c, config.bankroll_usd, config.max_stake_pct,
                                                          cfg=config.arb_sizing)
        # Research Signals: DIVERGENCE spreads, shown separately, no P&L (kept out of
        # open-bet totals entirely per the reporting rules).
        research_rows = [
            dict(r)
            for r in c.execute(
                """
                SELECT id, edge, sport, category, placed_at, notes, legs_json
                  FROM flat_bets
                 WHERE kind='DIVERGENCE' AND status='OPEN'
                 ORDER BY CAST(edge AS REAL) DESC
                 LIMIT 40
                """
            )
        ]
        # LIVE_BASEBALL_EV — isolated lane (LIVE_BASEBALL_EV_SPEC §2), never folded into clean
        # pregame totals. Counts by lifecycle/quarantine flag + settled paper P&L.
        live_counts = {
            (r["quality_flag"], r["status"]): int(r["n"])
            for r in c.execute(
                """SELECT quality_flag, status, COUNT(*) n FROM flat_bets
                    WHERE kind='LIVE_BASEBALL_EV' GROUP BY quality_flag, status"""
            )
        }
        live_pnl_row = c.execute(
            """SELECT COALESCE(SUM(CAST(pnl_optimistic AS REAL)),0) pnl, COUNT(*) n
                 FROM flat_bets WHERE kind='LIVE_BASEBALL_EV' AND status='SETTLED'"""
        ).fetchone()
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
                SELECT placed_at, pnl_optimistic, pnl_confirmed, quality_flag, kind, trusted, validation_tag
                  FROM flat_bets
                 WHERE status IN ('SETTLED','EXITED') AND result IN ('WON','LOST','RESOLVED','VOID','SOLD')
                 ORDER BY placed_at
                """
            )
        )
        processes = _process_health()
        scanner_summaries = _scanner_summaries(c, processes, now)
        settler_errors = c.execute(
            "SELECT COUNT(*) FROM flat_bets WHERE status='CORRECTED' AND result='SETTLER-ERROR'"
        ).fetchone()[0]

    def _is_headline(row) -> bool:
        # ONE canonical "headline / clean" definition, used by every aggregate so the hero
        # number, its lane chips, the equity curve, the per-strategy breakdown and attribution
        # all reconcile. A row counts toward the headline paper book iff:
        #   - it is not quarantined/thin (quality_flag IS NULL: excludes micro/suspect/below-e-min/stale),
        #   - it is not a REVIEW cross-venue arb (kind=ARB AND trusted=0 → provisional, hard rule #6),
        #     regardless of quality_flag (pre-A1-fix rows are NULL-flagged but still trusted=0), and
        #   - for EV, it passes the de-vig validation gate (validation_tag IS NULL or KEEP).
        if row["quality_flag"] is not None:
            return False
        if row["kind"] == "ARB" and not row["trusted"]:
            return False
        if row["kind"] == "EV" and row["validation_tag"] not in (None, "KEEP"):
            return False
        return True

    def curve(field: str, clean: bool) -> list[dict[str, Any]]:
        cum, out = 0.0, []
        for row in settled_rows:
            if clean and not _is_headline(row):
                continue
            if row[field] is None:
                continue
            cum += float(row[field])
            out.append({"t": row["placed_at"], "y": round(cum, 4)})
        return out

    def curve_by_kind() -> dict[str, list[dict[str, Any]]]:
        # Per-strategy cumulative headline optimistic P&L (same predicate as the hero), so the
        # By-strategy lines and attribution sum to the headline total. ARB here is trusted-only.
        cums: dict[str, float] = {}
        series: dict[str, list[dict[str, Any]]] = {}
        for row in settled_rows:
            if not _is_headline(row) or row["pnl_optimistic"] is None:
                continue
            kind = row["kind"] or "?"
            cums[kind] = cums.get(kind, 0.0) + float(row["pnl_optimistic"])
            series.setdefault(kind, []).append({"t": row["placed_at"], "y": round(cums[kind], 4)})
        return series

    def headline_summary() -> dict[str, Any]:
        # Authoritative headline totals + the lane breakdown that sums to them, plus the
        # explicitly-excluded buckets so the UI can show "what's NOT in this number" honestly.
        by_lane: dict[str, dict[str, float]] = {}
        opt = conf = 0.0
        excl_review = excl_review_n = 0.0
        excl_quarantined = excl_quarantined_n = 0.0
        for row in settled_rows:
            po = float(row["pnl_optimistic"]) if row["pnl_optimistic"] is not None else 0.0
            pc = float(row["pnl_confirmed"]) if row["pnl_confirmed"] is not None else 0.0
            if _is_headline(row):
                lane = by_lane.setdefault(row["kind"] or "?", {"optimistic": 0.0, "confirmed": 0.0, "n": 0})
                lane["optimistic"] += po
                lane["confirmed"] += pc
                lane["n"] += 1
                opt += po
                conf += pc
            elif row["kind"] == "ARB" and not row["trusted"]:
                excl_review += po
                excl_review_n += 1
            elif row["quality_flag"] is not None:
                excl_quarantined += po
                excl_quarantined_n += 1
        return {
            "optimistic": _money(opt),
            "confirmed": _money(conf),
            "by_lane": {k: {"optimistic": _money(v["optimistic"]), "confirmed": _money(v["confirmed"]),
                            "n": int(v["n"])} for k, v in sorted(by_lane.items())},
            "excluded_review_pnl": _money(excl_review),
            "excluded_review_n": int(excl_review_n),
            "excluded_quarantined_pnl": _money(excl_quarantined),
            "excluded_quarantined_n": int(excl_quarantined_n),
        }

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

    clv = _cached_clv(store, config)

    # Control plane — per-version P&L head-to-head where the ledger attributes it (lanes map to
    # a kind; pure model versions have no historical per-row tag, so their P&L stays null).
    pnl_by_kind: dict[str, float] = {str(r["kind"]): float(r.get("pnl") or 0.0) for r in kind_rows}
    for r in quality_rows:
        pnl_by_kind[f"{r['kind']}:{r['quality']}"] = float(r.get("pnl") or 0.0)
    controls = control.effective_controls(pnl_by_kind=pnl_by_kind)
    headline = headline_summary()
    # Attribution is the headline lane breakdown — same predicate as the hero, so the bars sum
    # to the headline total and ARB shows the trusted lane only (REVIEW is excluded, not hidden).
    attribution = sorted(
        [{"kind": k, "pnl": lane["optimistic"], "n": lane["n"]} for k, lane in headline["by_lane"].items()],
        key=lambda r: r["pnl"], reverse=True,
    )
    overlay_active = overlay_path().exists()

    won_n, won_p = int(settled.get("WON", {}).get("n", 0)), float(settled.get("WON", {}).get("pnl", 0.0))
    lost_n, lost_p = int(settled.get("LOST", {}).get("n", 0)), float(settled.get("LOST", {}).get("pnl", 0.0))
    resolved_p = float(settled.get("RESOLVED", {}).get("pnl", 0.0))
    last_poll_started = last_poll["started_at"] if last_poll else None
    last_poll_finished = last_poll["finished_at"] if last_poll else None
    seen_fingerprints: set[str] = set()
    open_rows = []
    negrisk_hidden = 0
    for row in raw_open_rows:
        # NEGRISK is ignored unless executable; non-executable baskets are hidden, counted.
        if row["kind"] == "NEGRISK" and not _negrisk_executable(row):
            negrisk_hidden += 1
            continue
        fingerprint = row["fingerprint"]
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        open_rows.append(row)
        if len(open_rows) >= 250:
            break
    # Capital exposure + a single alerts list so problems surface at the top, not buried.
    from arb.exposure import compute_exposure
    per_venue_cap = config.bankroll_usd * config.max_stake_pct
    exp = compute_exposure(store, config.bankroll_usd, per_venue_cap,
                           per_venue_caps=config.per_venue_caps,
                           sport_duration_hours=config.sport_duration_hours,
                           venue_settlement_lag_hours=config.venue_settlement_lag_hours)
    last_poll_age = _age_seconds(last_poll_finished or last_poll_started)
    alerts: list[dict[str, str]] = []
    for p in processes:
        if p["status"] == "duplicate":
            alerts.append({"level": "bad", "text": f"{p['name']} has {p['count']} copies running — double-counting bets"})
        elif p["status"] == "down":
            alerts.append({"level": "warn", "text": f"{p['name']} is not running"})
    if last_poll_age is not None and last_poll_age > 600:
        alerts.append({"level": "warn", "text": f"last poll was {last_poll_age // 60} min ago — scanner may be stalled"})
    if exp.over_bankroll:
        alerts.append({"level": "bad", "text": f"committed ${exp.total_committed} exceeds bankroll ${exp.bankroll}"})
    for venue in exp.over_venue:
        alerts.append({"level": "warn", "text": f"{venue} exposure ${exp.by_venue[venue]} over per-venue cap ${per_venue_cap}"})
    if settler_errors:
        alerts.append({"level": "bad", "text": f"{settler_errors} settlement mismatch(es) — realized P&L differs from the lock"})
    credits_left = credits[0] if credits else None
    if credits_left is not None and credits_left < 100:
        alerts.append({"level": "warn", "text": f"Odds API credits low: {credits_left} left"})
    # Derive the live-lane summary (counts keyed by quarantine flag set in record_live_candidate).
    def _live_n(flag):
        return sum(n for (qf, _status), n in live_counts.items() if qf == flag)
    live_confirmed = sum(n for (qf, _s), n in live_counts.items() if qf is None)
    live_vanished = _live_n("live-vanished")
    live_denom = live_confirmed + live_vanished
    live_baseball = {
        "confirmed": live_confirmed,
        "vanished": live_vanished,
        "vanished_rate": round(live_vanished / live_denom, 4) if live_denom else None,
        "research": _live_n("live-research"),
        "stale_rejected": _live_n("live-rejected_stale"),
        "suspension_rejected": _live_n("live-rejected_unstable"),
        "settled_pnl": round(float(live_pnl_row["pnl"]), 2),
        "settled_n": int(live_pnl_row["n"]),
        "total": sum(live_counts.values()),
    }

    def _edge_sized_payload(key: str) -> dict[str, Any]:
        baseline = EDGE_SIZED_EV_BASELINES[key]
        row = edge_sized_rows[key]
        settled_new = int(row["settled"] or 0)
        stake_new = float(row["stake"] or 0.0)
        pnl_new = float(row["pnl"] or 0.0)
        stake_total = float(baseline["stake"]) + stake_new
        pnl_total = float(baseline["pnl"]) + pnl_new
        return {
            "label": str(baseline["label"]),
            "filter_label": str(baseline["filter_label"]),
            "baseline_max_id": int(baseline["max_id"]),
            "baseline_settled": int(baseline["settled"]),
            "baseline_stake": _money(baseline["stake"]),
            "baseline_pnl": _money(baseline["pnl"]),
            "new_settled": settled_new,
            "new_won": int(row["won"] or 0),
            "new_lost": int(row["lost"] or 0),
            "new_stake": _money(stake_new),
            "new_pnl": _money(pnl_new),
            "new_confirmed_pnl": _money(row["confirmed_pnl"]),
            "settled": int(baseline["settled"]) + settled_new,
            "stake": _money(stake_total),
            "pnl": _money(pnl_total),
            "roi": round(pnl_total / stake_total, 4) if stake_total else 0.0,
            "avg_new_stake": _money(row["avg_stake"]),
            "max_new_stake": _money(row["max_stake"]),
            "max_id": int(row["max_id"] or baseline["max_id"]),
        }

    edge_sized_ev = {
        "rule": "$1 per 1% displayed edge",
        "variants": {
            "keep": _edge_sized_payload("keep"),
            "broad": _edge_sized_payload("broad"),
        },
    }
    return {
        "now": now.isoformat(),
        "alerts": alerts,
        "live_baseball": live_baseball,
        "exposure": {
            "committed": float(exp.total_committed), "bankroll": float(exp.bankroll),
            "over_bankroll": exp.over_bankroll, "by_venue": {v: float(a) for v, a in exp.by_venue.items()},
            "settler_errors": int(settler_errors),
        },
        "processes": processes,
        "scanners": scanner_summaries,
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
        # Clean EV P&L — the headline. EV judged alone (clean settled WON/LOST/VOID only).
        "clean_ev": {
            "settled": int(clean_ev["settled"] or 0),
            "won": int(clean_ev["won"] or 0),
            "lost": int(clean_ev["lost"] or 0),
            "sold": int(clean_ev["sold"] or 0),
            "pnl": _money(clean_ev["pnl"]),
            "stake": _money(clean_ev["stake"]),
            "roi": round((float(clean_ev["pnl"] or 0) / float(clean_ev["stake"])), 4) if float(clean_ev["stake"] or 0) else 0.0,
            "win_rate": (
                round(int(clean_ev["won"] or 0) / (int(clean_ev["won"] or 0) + int(clean_ev["lost"] or 0)), 4)
                if int(clean_ev["won"] or 0) + int(clean_ev["lost"] or 0) else 0.0
            ),
        },
        "edge_sized_ev": edge_sized_ev,
        "suspect_ev": {"n": int(suspect_ev["n"] or 0), "pnl": _money(suspect_ev["pnl"])},
        "rejected_ev": {"n": int(rejected_ev["n"] or 0)},
        "micro_ev": {
            "active": int(micro_active["n"] or 0),
            "active_stake": _money(micro_active["stake"]),
            "settled": int(micro_ev["settled"] or 0),
            "won": int(micro_ev["won"] or 0),
            "lost": int(micro_ev["lost"] or 0),
            "sold": int(micro_ev["sold"] or 0),
            "pnl": _money(micro_ev["pnl"]),
            "stake": _money(micro_ev["stake"]),
            "roi": round((float(micro_ev["pnl"] or 0) / float(micro_ev["stake"])), 4) if float(micro_ev["stake"] or 0) else 0.0,
            "win_rate": (
                round(int(micro_ev["won"] or 0) / (int(micro_ev["won"] or 0) + int(micro_ev["lost"] or 0)), 4)
                if int(micro_ev["won"] or 0) + int(micro_ev["lost"] or 0) else 0.0
            ),
        },
        # Locked ARB P&L — ARB's own lane (locked/provisional, includes RESOLVED).
        # trusted = whitelisted clean lane; review = trusted=0 (hard rule #6, provisional only).
        "locked_arb": {
            "settled": int(locked_arb["settled"] or 0),
            "resolved": int(locked_arb["resolved"] or 0),
            "pnl": _money(locked_arb["pnl"]),
            "trusted_settled": int(locked_arb["trusted_settled"] or 0),
            "trusted_pnl": _money(locked_arb["trusted_pnl"]),
            "review_settled": int(locked_arb["review_settled"] or 0),
            "review_pnl": _money(locked_arb["review_pnl"]),
            "invalid_in_play": int(invalid_arb["n"] or 0),
        },
        # Research Signals — DIVERGENCE spreads, no P&L, not bets.
        "research": {
            "open": int(research_open),
            "rows": [
                {"id": int(r["id"]), "edge": round(float(r["edge"] or 0), 4),
                 "sport": r["sport"], "category": r["category"], "placed_at": r["placed_at"]}
                for r in research_rows
            ],
        },
        "negrisk_hidden": int(negrisk_hidden),
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
        "controls": controls,
        "config_overlay_active": overlay_active,
        "headline": headline,
        "attribution": attribution,
        "equity_by_kind": curve_by_kind(),
        "arb_sizing_model": arb_sizing_model,
        "arb_scale_projection": arb_scale_projection,
        "open_bets": [_open_bet_payload(row, now, arb_sizing_model) for row in open_rows],
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
        elif self.path.startswith("/api/config"):
            _send_json(self, {"ok": True, "controls": control.effective_controls(),
                              "overlay_active": overlay_path().exists()})
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

    def do_POST(self):  # noqa: N802
        if self.path not in ("/api/scanner", "/api/config"):
            self.send_response(404)
            self.end_headers()
            return
        # Same-origin guard: the server is localhost-bound, but reject cross-site POSTs too.
        origin = self.headers.get("Origin")
        if origin and not (origin.endswith("//localhost:" + str(self.server.server_address[1]))
                           or origin.endswith("//127.0.0.1:" + str(self.server.server_address[1]))):
            _send_json(self, {"ok": False, "error": "cross-origin POST refused"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/config":
                result = control.set_control(str(payload.get("id", "")), payload.get("value"))
            else:
                result = _set_scanner_state(str(payload.get("scanner", "")), str(payload.get("action", "")))
            if not result.get("ok"):
                _send_json(self, result, 400)
                return
            result["state"] = build_state(self.store, self.config)
            _send_json(self, result)
        except Exception as exc:  # noqa: BLE001
            _send_json(self, {"ok": False, "error": str(exc)}, 500)


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
  /* dark (default) */
  --bg:#111315;--panel:#1a1d21;--panel-2:#22262b;--panel-3:#15181c;--line:#343a42;--line-soft:#282d34;
  --text:#f1f3f0;--muted:#a9afb7;--muted-2:#808892;
  --green:#61c88a;--red:#ff7a70;--gold:#e5b85a;--blue:#76aefc;--violet:#bca2ff;--cyan:#6fd5ce;
  --gap:14px;--radius:8px;--fs:14px;--pad:14px;--metric-fs:26px;
  --grid-soft:#303238;--axis:#a4a6ad;--top-bg:rgba(17,19,21,.96);--shadow:0 1px 0 rgba(255,255,255,.03);
  --accent:var(--blue);
}
:root[data-theme="dim"]{
  --bg:#161a21;--panel:#1d222b;--panel-2:#252b35;--panel-3:#1a1f27;--line:#39414d;--line-soft:#2b323c;
  --text:#eef1f6;--muted:#aeb6c2;--muted-2:#838b97;--grid-soft:#333b46;--axis:#aeb6c2;--top-bg:rgba(22,26,33,.96);
}
:root[data-theme="light"]{
  --bg:#f4f6f9;--panel:#ffffff;--panel-2:#eef1f5;--panel-3:#f7f9fb;--line:#cdd4dd;--line-soft:#e1e6ec;
  --text:#1a1f26;--muted:#5a626d;--muted-2:#828a95;
  --green:#1f9d57;--red:#d23b35;--gold:#b9821a;--blue:#2f6fd6;--violet:#7a55d6;--cyan:#1f978c;
  --grid-soft:#dfe4ea;--axis:#6a727d;--top-bg:rgba(244,246,249,.94);--shadow:0 1px 2px rgba(20,30,50,.06);
}
:root[data-density="compact"]{--gap:9px;--pad:10px;--fs:13px;--metric-fs:22px;--radius:7px}
*{box-sizing:border-box}
*::-webkit-scrollbar{width:11px;height:11px}
*::-webkit-scrollbar-thumb{background:var(--line);border-radius:99px;border:3px solid transparent;background-clip:content-box}
*::-webkit-scrollbar-thumb:hover{background:var(--muted-2)}
*::-webkit-scrollbar-track{background:transparent}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
html{scroll-behavior:smooth;scroll-padding-top:84px}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{transition:none!important;animation:none!important}}
body{margin:0;background:var(--bg);color:var(--text);font:var(--fs)/1.48 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow-anchor:none;transition:background .2s ease,color .2s ease}
button{font:inherit}
.top{position:sticky;top:0;z-index:20;background:var(--top-bg);border-bottom:1px solid var(--line-soft);backdrop-filter:blur(10px)}
.top-inner{display:flex;align-items:center;justify-content:space-between;gap:18px;max-width:1480px;margin:0 auto;padding:16px 22px}
.brand{display:flex;align-items:baseline;gap:12px;min-width:260px}
h1{font-size:18px;line-height:1;margin:0;font-weight:720;letter-spacing:0}
.sub{color:var(--muted);font-size:13px}
.procs{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}
.proc{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line-soft);background:var(--panel-2);height:28px;padding:0 9px;border-radius:var(--radius);color:var(--muted);font-size:12px}
.dot{width:8px;height:8px;border-radius:99px;background:var(--muted)}
.ok .dot{background:var(--green);box-shadow:0 0 10px rgba(86,194,113,.6)}
.down .dot{background:var(--red)}
.duplicate .dot{background:var(--gold)}
.wrap{max-width:1480px;margin:0 auto;padding:18px 22px 30px}
.grid,.panels{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:var(--gap)}
.panel{background:var(--panel);border:1px solid var(--line-soft);border-radius:var(--radius);min-width:0;box-shadow:var(--shadow);overflow-anchor:none;scroll-margin-top:84px}
.panel.collapsed .panel-body{display:none}
.panels{margin-top:var(--gap);align-items:start}
.panels>.panel{container-type:inline-size}
.metric{grid-column:span 3;padding:var(--pad) calc(var(--pad) + 1px);min-height:112px;display:flex;flex-direction:column;justify-content:space-between;position:relative}
.metric.clickable{cursor:pointer}
.metric.clickable:hover{border-color:#555963}
.metric .spark{position:absolute;right:12px;top:12px;opacity:.85}
.wide{grid-column:span 8}.side{grid-column:span 4}.half{grid-column:span 6}.full{grid-column:span 12}
.label{color:var(--muted-2);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:7px}
.value{font-size:var(--metric-fs);line-height:1.08;font-weight:760;letter-spacing:0;white-space:nowrap}
.unit{font-size:12px;color:var(--muted);font-weight:520;margin-left:5px}
.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--gold)}.blue{color:var(--blue)}.muted{color:var(--muted)}
#alerts{display:flex;flex-direction:column;gap:6px;margin-bottom:12px}
.alert{padding:8px 12px;border-radius:8px;font-size:13px;font-weight:500;border:1px solid}
.alert.bad{background:rgba(229,72,77,.12);border-color:rgba(229,72,77,.4);color:var(--red)}
.alert.warn{background:rgba(214,158,46,.12);border-color:rgba(214,158,46,.35);color:var(--gold)}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:54px;padding:12px 14px;border-bottom:1px solid var(--line-soft)}
.panel-title{font-size:13px;font-weight:700;letter-spacing:0;display:flex;align-items:center;gap:7px}
.panel-body{padding:var(--pad)}
.collapse-btn{appearance:none;border:1px solid var(--line-soft);background:var(--panel-2);color:var(--muted);width:26px;height:26px;border-radius:7px;cursor:pointer;font-size:13px;line-height:1;display:inline-flex;align-items:center;justify-content:center}
.collapse-btn:hover{border-color:#555963;color:var(--text)}
.info{display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border-radius:99px;border:1px solid var(--line);color:var(--muted-2);font-size:10px;font-weight:700;cursor:help;flex:0 0 auto;user-select:none}
.info:hover{color:var(--text);border-color:var(--accent)}
th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
th.sortable:hover{color:var(--text)}
th.sortable .arrow{opacity:.5;font-size:9px;margin-left:2px}
.scroll-tbl{overflow:auto}
.scroll-tbl thead th{position:sticky;top:0;background:var(--panel);z-index:2}
.chart{width:100%;height:260px;display:block}
.mini{height:150px}
.bars{display:grid;gap:9px}
.bar-row{display:grid;grid-template-columns:92px 1fr 42px;align-items:center;gap:10px;font-size:12px}
.bar-track{height:9px;background:var(--panel-3);border:1px solid var(--line-soft);border-radius:99px;overflow:hidden}
.bar-fill{height:100%;background:var(--blue)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:8px 7px;border-bottom:1px solid var(--line-soft);text-align:left;vertical-align:middle}
th{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:650}
tr:last-child td{border-bottom:0}
.badge{display:inline-flex;align-items:center;height:22px;padding:0 7px;border-radius:6px;font-size:11px;font-weight:740;background:#25262c;color:var(--muted)}
.badge.OPEN,.badge.REVIEW{color:var(--blue);background:rgba(106,168,255,.13)}
.badge.VERIFIED,.badge.CONFIRMED,.badge.WON{color:var(--green);background:rgba(86,194,113,.14)}
.badge.VANISHED,.badge.LOST,.badge.BLOCKED{color:var(--red);background:rgba(255,107,99,.14)}
.badge.LIFETIME,.badge.SETTLED,.badge.RESOLVED,.badge.EXITED,.badge.SOLD{color:var(--violet);background:rgba(181,140,255,.13)}
.badge.UNVERIFIABLE,.badge.INSUFFICIENT_DATA{color:var(--gold);background:rgba(224,173,59,.13)}
.badge.MICRO_EV,.badge.micro{color:#5fe0c1;background:rgba(95,224,193,.14)}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{height:28px;border-radius:7px;border:1px solid var(--line-soft);background:var(--panel-2);color:var(--muted);padding:0 9px;cursor:pointer}
.tab.active{color:var(--text);border-color:#555963;background:#2a2c33}
.log{margin:0;height:320px;overflow:auto;background:var(--panel-3);border:1px solid var(--line-soft);border-radius:8px;padding:12px;color:#d2d5d0;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap}
.status-line{display:flex;align-items:center;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px}
.pill{display:inline-flex;align-items:center;height:24px;padding:0 8px;border:1px solid var(--line-soft);border-radius:99px;background:var(--panel-2);color:var(--muted)}
.action{height:30px;border-radius:7px;border:1px solid var(--line-soft);background:var(--panel-2);color:var(--text);padding:0 10px;cursor:pointer}
.action:hover{border-color:#555963;background:#2a2c33}
.track-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px}
.track-card{background:var(--panel-3);border:1px solid var(--line-soft);border-radius:8px;padding:11px 12px;min-width:0}
.track-card .label{margin-bottom:5px}
.track-card .value{font-size:21px}
.scanner-list{display:grid;gap:8px;max-height:430px;overflow:auto;padding-right:3px}
.scanner-row{display:grid;grid-template-columns:auto 1fr;gap:8px 10px;align-items:center;background:var(--panel-3);border:1px solid var(--line-soft);border-radius:8px;padding:10px;min-width:0}
.scanner-row>div:nth-child(n+3){grid-column:1/-1}
.scanner-title{display:flex;align-items:center;gap:8px;font-weight:730;min-width:0}
.scanner-title span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scanner-meta{color:var(--muted);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scanner-toggle{height:30px;min-width:78px;border-radius:7px;border:1px solid var(--line-soft);background:var(--panel-2);color:var(--text);cursor:pointer}
.scanner-toggle.stop{border-color:rgba(255,107,99,.45);color:var(--red)}
.scanner-toggle.start{border-color:rgba(86,194,113,.45);color:var(--green)}
.scanner-toggle.linked{border-color:var(--line-soft);color:var(--muted);cursor:default}
.scanner-toggle:disabled{opacity:.55;cursor:wait}
.status-chip{display:inline-flex;align-items:center;gap:6px;height:24px;border:1px solid var(--line-soft);border-radius:99px;padding:0 8px;color:var(--muted);font-size:11px}
.status-chip .dot{width:7px;height:7px}
.status-chip.ok{color:var(--green)}.status-chip.down{color:var(--red)}.status-chip.disabled{color:var(--muted)}.status-chip.stopping,.status-chip.duplicate{color:var(--gold)}
.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.detail-block{min-width:0}
.detail-title{font-size:12px;font-weight:700;margin:0 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.delta-table td:nth-child(n+2),.delta-table th:nth-child(n+2){text-align:right}
.open-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.open-groups{display:grid;gap:14px;max-height:560px;overflow:auto;padding-right:3px}
.open-group-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 7px;color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.open-table td{font-size:12px}
.open-event{font-weight:700;color:var(--text);max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.subline{color:var(--muted);font-size:11px;margin-top:2px;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
#kinds,#recent{max-height:360px;overflow:auto;padding-right:3px}
@container (min-width:680px){.scanner-row{grid-template-columns:132px minmax(170px,1.3fr) minmax(150px,1fr) minmax(120px,.7fr) minmax(140px,.8fr)}.scanner-row>div:nth-child(n+3){grid-column:auto}}
@media (max-width:1180px){.metric{grid-column:span 4}.track-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.top-inner{align-items:flex-start;flex-direction:column}.procs{justify-content:flex-start}}
@media (max-width:900px){.wide,.side,.half{grid-column:span 12}.detail-grid{grid-template-columns:1fr}}
@media (max-width:820px){.metric{grid-column:span 6}.track-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:700px){.wrap{padding:12px}.top-inner{padding:12px}.value{font-size:22px}.chart{height:230px}.log{height:260px}.scanner-row{grid-template-columns:1fr}.scanner-row>div:nth-child(n){grid-column:auto}th:nth-child(5),td:nth-child(5){display:none}}

/* ---- Comfort & control layer ---- */
.controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ctl{display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 10px;border:1px solid var(--line-soft);background:var(--panel-2);color:var(--muted);border-radius:8px;cursor:pointer;font-size:12px;white-space:nowrap}
.ctl:hover{border-color:#555963;color:var(--text)}
.ctl.on{color:var(--text);border-color:var(--accent)}
.ctl .dot{width:8px;height:8px;border-radius:99px;background:var(--muted)}
.ctl.live .dot{background:var(--green);box-shadow:0 0 9px rgba(97,200,138,.7);animation:pulse 2s ease-in-out infinite}
.ctl.paused .dot{background:var(--gold)}
.ctl.stale .dot{background:var(--gold)}
.ctl.offline .dot{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.seg{display:inline-flex;border:1px solid var(--line-soft);border-radius:8px;overflow:hidden;background:var(--panel-2)}
.seg button{appearance:none;border:0;background:transparent;color:var(--muted);height:28px;padding:0 9px;cursor:pointer;font-size:12px;border-right:1px solid var(--line-soft)}
.seg button:last-child{border-right:0}
.seg button.active{background:var(--accent);color:#0c1116;font-weight:700}
.seg button:hover:not(.active){color:var(--text)}
.icon-btn{appearance:none;width:30px;height:30px;border:1px solid var(--line-soft);background:var(--panel-2);color:var(--muted);border-radius:8px;cursor:pointer;font-size:14px;display:inline-flex;align-items:center;justify-content:center}
.icon-btn:hover{border-color:#555963;color:var(--text)}
/* toasts */
#toasts{position:fixed;right:18px;bottom:18px;z-index:60;display:flex;flex-direction:column;gap:8px;max-width:360px}
.toast{padding:11px 13px;border-radius:9px;border:1px solid var(--line);background:var(--panel-2);color:var(--text);font-size:13px;box-shadow:0 8px 28px rgba(0,0,0,.35);opacity:0;transform:translateY(8px);transition:opacity .18s,transform .18s;display:flex;gap:9px;align-items:flex-start}
.toast.show{opacity:1;transform:none}
.toast.bad{border-color:rgba(255,107,99,.5)}.toast.good{border-color:rgba(97,200,138,.5)}.toast.warn{border-color:rgba(229,184,90,.5)}
.toast b{font-weight:700}
.toast .x{margin-left:auto;cursor:pointer;color:var(--muted);background:none;border:0;font-size:14px}
/* nav drawer + overlays */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:50;display:none;align-items:flex-start;justify-content:center;padding:64px 18px;backdrop-filter:blur(2px)}
.overlay.open{display:flex}
.sheet{background:var(--panel);border:1px solid var(--line);border-radius:12px;max-width:560px;width:100%;max-height:80vh;overflow:auto;box-shadow:0 18px 60px rgba(0,0,0,.5)}
.sheet-head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--line-soft);position:sticky;top:0;background:var(--panel)}
.sheet-head h2{margin:0;font-size:15px}
.sheet-body{padding:14px 16px}
.navlist{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.navlist a{display:flex;align-items:center;gap:9px;padding:10px 12px;border:1px solid var(--line-soft);border-radius:9px;color:var(--text);text-decoration:none;background:var(--panel-2);font-size:13px}
.navlist a:hover{border-color:var(--accent)}
.navlist a kbd{margin-left:auto}
kbd{font:11px ui-monospace,Menlo,monospace;background:var(--panel-3);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 6px;color:var(--muted)}
.keytable{width:100%;font-size:13px}
.keytable td{padding:7px 4px;border-bottom:1px solid var(--line-soft)}
.keytable td:first-child{width:120px}
.search{height:30px;border:1px solid var(--line-soft);background:var(--panel-3);color:var(--text);border-radius:8px;padding:0 10px;font-size:12.5px;min-width:160px}
.search:focus{border-color:var(--accent);outline:none}
.chiprow{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.hl{background:rgba(118,174,252,.28);border-radius:3px;padding:0 1px}
/* skeleton */
.skel{position:relative;overflow:hidden;background:var(--panel-2);border-radius:6px;min-height:14px}
.skel::after{content:"";position:absolute;inset:0;transform:translateX(-100%);background:linear-gradient(90deg,transparent,rgba(255,255,255,.07),transparent);animation:shimmer 1.3s infinite}
@keyframes shimmer{100%{transform:translateX(100%)}}
.split-bar{display:flex;height:7px;border-radius:99px;overflow:hidden;background:var(--panel-3);border:1px solid var(--line-soft);margin-top:8px}
.split-bar span{height:100%}
.tip{position:relative}
.miniflag{display:inline-block;font-size:10px;font-weight:700;padding:1px 5px;border-radius:5px;background:rgba(229,184,90,.16);color:var(--gold);margin-left:6px;vertical-align:middle}
/* control plane */
.ctrl-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-bottom:8px}
.ctrl-row{display:grid;grid-template-columns:1fr auto;gap:10px 14px;align-items:center;background:var(--panel-3);border:1px solid var(--line-soft);border-radius:9px;padding:11px 13px;min-width:0}
.ctrl-row.betting{border-color:rgba(229,184,90,.5);background:rgba(229,184,90,.07)}
.ctrl-meta{min-width:0}
.ctrl-name{font-weight:700;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.ctrl-desc{color:var(--muted);font-size:11.5px;margin-top:3px;line-height:1.4}
.ctrl-pnl{font-size:11px;margin-top:5px;display:inline-block}
.src{font-size:9.5px;font-weight:700;padding:1px 6px;border-radius:5px;background:var(--panel-2);color:var(--muted-2);border:1px solid var(--line-soft);text-transform:uppercase;letter-spacing:.04em}
.src.overlay{color:var(--blue);border-color:rgba(118,174,252,.5)}
.switch{position:relative;width:42px;height:24px;flex:0 0 auto;display:inline-block}
.switch input{opacity:0;width:0;height:0;position:absolute}
.switch .track{position:absolute;inset:0;background:var(--panel-2);border:1px solid var(--line);border-radius:99px;transition:.15s;cursor:pointer}
.switch .knob{position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:99px;background:var(--muted);transition:.15s}
.switch input:checked+.track{background:rgba(97,200,138,.28);border-color:var(--green)}
.switch input:checked+.track .knob{left:20px;background:var(--green)}
.switch input:disabled+.track{opacity:.5;cursor:not-allowed}
.ctrl-select{height:30px;border-radius:7px;border:1px solid var(--line-soft);background:var(--panel-2);color:var(--text);padding:0 8px;cursor:pointer}
.ctrl-num{width:92px;height:30px;border-radius:7px;border:1px solid var(--line-soft);background:var(--panel-3);color:var(--text);padding:0 8px}
.lock{font-size:11px;color:var(--gold);white-space:nowrap}
.attrib{display:grid;gap:6px;margin-top:12px}
.attrib-row{display:grid;grid-template-columns:96px 1fr 96px;align-items:center;gap:10px;font-size:12px}
.attrib-track{height:14px;position:relative;background:var(--panel-3);border:1px solid var(--line-soft);border-radius:5px}
.attrib-fill{position:absolute;top:0;bottom:0;border-radius:4px}
.attrib-zero{position:absolute;top:-2px;bottom:-2px;width:1px;background:var(--muted-2);opacity:.6}
@media (max-width:760px){.ctrl-grid{grid-template-columns:1fr}}
@media (max-width:700px){.navlist{grid-template-columns:1fr}#toasts{left:12px;right:12px;max-width:none}.controls .hide-sm{display:none}}
</style>
</head>
<body>
<header class="top">
  <div class="top-inner">
    <div class="brand">
      <h1>ArbitrageModel</h1>
      <div class="sub" id="clock">connecting…</div>
    </div>
    <div class="controls">
      <button class="ctl live" id="refresh-toggle" title="Pause / resume auto-refresh (Space)">
        <span class="dot"></span><span id="refresh-label">live</span>
      </button>
      <div class="seg hide-sm" id="interval-seg" title="Auto-refresh interval">
        <button data-int="2000">2s</button><button data-int="5000" class="active">5s</button><button data-int="10000">10s</button><button data-int="30000">30s</button>
      </div>
      <button class="icon-btn" id="nav-btn" title="Jump to section (g)">☰</button>
      <button class="icon-btn" id="theme-btn" title="Theme (t)">◐</button>
      <button class="icon-btn hide-sm" id="density-btn" title="Density (d)">⇲</button>
      <button class="icon-btn" id="help-btn" title="Keyboard shortcuts (?)">?</button>
    </div>
    <div class="procs" id="procs"></div>
  </div>
</header>
<div id="toasts"></div>
<div class="overlay" id="nav-overlay">
  <div class="sheet" role="dialog" aria-label="Jump to section">
    <div class="sheet-head"><h2>Jump to section</h2><button class="icon-btn" data-close>✕</button></div>
    <div class="sheet-body"><div class="navlist" id="navlist"></div></div>
  </div>
</div>
<div class="overlay" id="help-overlay">
  <div class="sheet" role="dialog" aria-label="Keyboard shortcuts">
    <div class="sheet-head"><h2>Keyboard shortcuts &amp; legend</h2><button class="icon-btn" data-close>✕</button></div>
    <div class="sheet-body">
      <table class="keytable"><tbody>
        <tr><td><kbd>Space</kbd></td><td>Pause / resume auto-refresh</td></tr>
        <tr><td><kbd>r</kbd></td><td>Refresh now</td></tr>
        <tr><td><kbd>g</kbd></td><td>Open the section jump menu</td></tr>
        <tr><td><kbd>t</kbd></td><td>Cycle theme (dark → dim → light)</td></tr>
        <tr><td><kbd>d</kbd></td><td>Toggle comfortable / compact density</td></tr>
        <tr><td><kbd>/</kbd></td><td>Focus the open-bets search</td></tr>
        <tr><td><kbd>?</kbd></td><td>This help</td></tr>
        <tr><td><kbd>Esc</kbd></td><td>Close any overlay</td></tr>
      </tbody></table>
      <div class="detail-title" style="margin-top:16px">Lane legend</div>
      <table class="keytable"><tbody>
        <tr><td><span class="badge OPEN">EV</span></td><td><b>Clean EV</b> — the headline paper track; clean, untagged or KEEP-validated.</td></tr>
        <tr><td><span class="badge MICRO_EV">MICRO</span></td><td><b>Micro EV</b> — thin-book ($5–$50 depth), depth-capped stake, kept off the headline.</td></tr>
        <tr><td><span class="badge SETTLED">ARB</span></td><td><b>Locked ARB</b> — cross-venue lock. <b>Trusted</b> = whitelisted clean lane; <b>REVIEW</b> = provisional only (hard rule #6), never real money until both rulebooks confirm.</td></tr>
        <tr><td><span class="badge VANISHED">X</span></td><td><b>Vanished</b> — the flagged price was gone on recheck (not capturable).</td></tr>
        <tr><td><span class="badge UNVERIFIABLE">?</span></td><td><b>Unverifiable</b> — could not be fast-rechecked (e.g. SX legs).</td></tr>
      </tbody></table>
    </div>
  </div>
</div>
<main class="wrap">
  <div id="alerts"></div>
  <section class="panels" style="margin-top:0">
    <div class="panel full" id="sec-hero">
      <div class="panel-head">
        <div class="panel-title">Total Paper Profit <span class="info" title="Cumulative settled paper P&amp;L. 'Clean' excludes quarantined/REVIEW rows; 'confirmed' uses the second-observation recheck price (the honest baseline). ARB REVIEW locks are provisional and shown separately — they are not real money (hard rule #6).">i</span></div>
        <div class="seg" id="hero-seg" title="Profit variant">
          <button data-variant="confirmed_clean" class="active">Clean · confirmed</button>
          <button data-variant="optimistic_clean">Clean · optimistic</button>
          <button data-variant="optimistic_raw">Raw · optimistic</button>
        </div>
      </div>
      <div class="panel-body"><div id="hero"></div></div>
    </div>
  </section>
  <section class="grid" id="metrics"></section>
  <section class="panels">
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
      <div class="panel-head">
        <div>
          <div class="panel-title">Clean EV</div>
          <div class="muted" id="clean-ev-summary"></div>
        </div>
        <div class="tabs" id="clean-ev-tabs"></div>
      </div>
      <div class="panel-body"><div class="track-grid" id="clean-ev-panel"></div></div>
    </div>
    <div class="panel full">
      <div class="panel-head">
        <div>
          <div class="panel-title">Scanning Controls</div>
          <div class="muted" id="scanner-summary"></div>
        </div>
        <div class="status-line"><span class="pill">start/stop workers</span><span class="pill">review-only where wording risk exists</span></div>
      </div>
      <div class="panel-body"><div class="scanner-list" id="scanners"></div></div>
    </div>
    <div class="panel full">
      <div class="panel-head">
        <div>
          <div class="panel-title">Strategy &amp; Version Controls <span class="info" title="Toggle strategy lanes and model versions. Writes go only to config.local.yaml (a merged overlay) — the canonical config.yaml is never touched, and reverting is deleting the key. Most changes need the named worker restarted. Betting flags are read-only here: this is a paper-only instrument.">i</span></div>
          <div class="muted" id="controls-summary"></div>
        </div>
        <div class="status-line"><span class="pill" id="overlay-pill">overlay</span><span class="pill">measurement-only · no orders</span></div>
      </div>
      <div class="panel-body"><div id="controls"></div></div>
    </div>
    <div class="panel full">
      <div class="panel-head open-toolbar">
        <div>
          <div class="panel-title">Open Bets &amp; EV</div>
          <div class="muted" id="open-summary"></div>
        </div>
        <div class="controls">
          <input class="search" id="open-search" type="search" placeholder="Filter event / venue / id…  ( / )" autocomplete="off">
          <div class="tabs" id="open-tabs"></div>
        </div>
      </div>
      <div class="panel-body"><div class="open-groups" id="open-bets"></div></div>
    </div>
    <div class="panel full">
      <div class="panel-head">
        <div>
          <div class="panel-title">ARB Sizing</div>
          <div class="muted" id="arb-sizing-summary"></div>
        </div>
        <div class="tabs" id="arb-sizing-tabs"></div>
      </div>
      <div class="panel-body">
        <div class="track-grid" id="arb-sizing-cards"></div>
        <div id="arb-sizing-detail" style="margin-top:12px"></div>
      </div>
    </div>
    <div class="panel wide">
      <div class="panel-head">
        <div>
          <div class="panel-title">Settled P&L</div>
          <div class="muted" id="equity-summary"></div>
        </div>
        <div class="tabs" id="equity-tabs"></div>
      </div>
      <div class="panel-body"><svg class="chart" id="equity"></svg><div class="attrib" id="attribution"></div></div>
    </div>
    <div class="panel side">
      <div class="panel-head"><div class="panel-title">Bet Pipeline</div><div class="muted" id="pipeline-total"></div></div>
      <div class="panel-body"><div class="bars" id="pipeline"></div></div>
    </div>
    <div class="panel half">
      <div class="panel-head"><div class="panel-title">Strategy Effectiveness</div><div class="muted" id="clv"></div></div>
      <div class="panel-body" id="kinds"></div>
    </div>
    <div class="panel half">
      <div class="panel-head"><div class="panel-title">Recent Bets</div><div class="muted">append-only ledger</div></div>
      <div class="panel-body" id="recent"></div>
    </div>
    <div class="panel half">
      <div class="panel-head"><div class="panel-title">Live Baseball EV</div><div class="muted">measurement-only · betting OFF · isolated lane</div></div>
      <div class="panel-body" id="live-baseball"></div>
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
let activeCleanEvMode = "flat";
let activeArbSizingMode = "model";
let activeHeroVariant = "confirmed_clean";
let activeEquityMode = "headline";
let openSearch = "";
const KIND_COLORS = {EV:"#76aefc", ARB:"#bca2ff", NEGRISK:"#6fd5ce", LIVE_BASEBALL_EV:"#e5b85a", DIVERGENCE:"#a4a6ad", PARLAY:"#ff7a70"};
const kindColor = k => KIND_COLORS[k] || "#9aa0aa";
const colors = {raw:"#a4a6ad", clean:"#6aa8ff", confirmed:"#56c271", opps:"#e0ad3b"};
const trackKey = "arb-dashboard-tracking-v1";
const laneLabel = kind => ({MICRO_EV:"MICRO EV"}[kind] || kind);
const esc = v => String(v ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const num = v => Number(v || 0);
const compact = v => new Intl.NumberFormat(undefined,{notation:"compact",maximumFractionDigits:1}).format(num(v));
const money = v => `${num(v) >= 0 ? "+$" : "-$"}${Math.abs(num(v)).toFixed(2)}`;
const usd = v => `$${Math.abs(num(v)).toFixed(2)}`;
const pct = v => `${(num(v) * 100).toFixed(1)}%`;
const axisMoney = v => {
  const n = num(v), sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  return `${sign}$${a >= 1000 ? compact(a) : a.toFixed(a >= 10 ? 0 : 2)}`;
};
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

/* ---------- Comfort & control layer ---------- */
const SETTINGS_KEY = "arb-dashboard-settings-v1";
const COLLAPSE_KEY = "arb-dashboard-collapsed-v1";
const THEMES = ["dark", "dim", "light"];
const settings = (() => {
  let s = {};
  try { s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"); } catch (_) { s = {}; }
  return Object.assign({theme: "dark", density: "comfortable", intervalMs: 5000, paused: false}, s);
})();
function saveSettings() { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); }
function applySettings() {
  document.documentElement.dataset.theme = settings.theme;
  document.documentElement.dataset.density = settings.density === "compact" ? "compact" : "comfortable";
  document.querySelectorAll("#interval-seg button").forEach(b =>
    b.classList.toggle("active", Number(b.dataset.int) === settings.intervalMs));
  const dn = $("density-btn"); if (dn) dn.classList.toggle("on", settings.density === "compact");
}
let collapsed = (() => { try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]")); } catch (_) { return new Set(); } })();
function saveCollapsed() { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsed])); }

let lastUpdate = 0, connection = "connecting";
let refreshTimer = null;

function toast(message, level = "warn", ttl = 6000) {
  const box = $("toasts"); if (!box) return;
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.innerHTML = `<div>${esc(message)}</div><button class="x" title="dismiss">✕</button>`;
  el.querySelector(".x").onclick = () => el.remove();
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  if (ttl) setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 220); }, ttl);
}

function openOverlay(id) { const o = $(id); if (o) o.classList.add("open"); }
function closeOverlays() { document.querySelectorAll(".overlay.open").forEach(o => o.classList.remove("open")); }

// Section labels for the jump menu, in document order. Built lazily once panels exist.
const SECTION_LABELS = {
  "metrics": "Overview metrics", "sec-hero": "Total profit",
};
function panelSlug(panel, i) {
  const t = panel.querySelector(".panel-title");
  const base = (t ? t.textContent : `section-${i}`).trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
  return `sec-${base || i}`;
}
function decoratePanels() {
  document.querySelectorAll("main .panel").forEach((panel, i) => {
    if (!panel.id) panel.id = panelSlug(panel, i);
    const head = panel.querySelector(".panel-head");
    const titleEl = panel.querySelector(".panel-title");
    if (head && titleEl && !head.querySelector(".collapse-btn")) {
      const btn = document.createElement("button");
      btn.className = "collapse-btn"; btn.type = "button"; btn.title = "Collapse / expand section";
      btn.textContent = collapsed.has(panel.id) ? "+" : "–";
      if (collapsed.has(panel.id)) panel.classList.add("collapsed");
      btn.onclick = () => {
        const now = panel.classList.toggle("collapsed");
        btn.textContent = now ? "+" : "–";
        if (now) collapsed.add(panel.id); else collapsed.delete(panel.id);
        saveCollapsed();
      };
      // place the collapse control at the far right of the header
      head.appendChild(btn);
    }
  });
}
function buildNav() {
  const list = $("navlist"); if (!list) return;
  const items = [];
  const metrics = $("metrics"); if (metrics) items.push(["metrics", "Overview metrics"]);
  document.querySelectorAll("main .panel").forEach(panel => {
    const t = panel.querySelector(".panel-title");
    if (t) items.push([panel.id, t.textContent.trim()]);
  });
  list.innerHTML = items.map(([id, label]) =>
    `<a href="#${esc(id)}" data-nav="${esc(id)}">${esc(label)}</a>`).join("");
  list.querySelectorAll("a").forEach(a => a.onclick = () => {
    closeOverlays();
    const el = $(a.dataset.nav); if (el) { el.classList.remove("collapsed"); }
  });
}

function setPaused(paused) {
  settings.paused = paused; saveSettings();
  if (paused) { if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; } }
  else { scheduleNext(0); }
  updateRefreshChip();
}
function updateRefreshChip() {
  const chip = $("refresh-toggle"), label = $("refresh-label");
  if (!chip || !label) return;
  chip.classList.remove("live", "paused", "stale", "offline");
  if (settings.paused) { chip.classList.add("paused"); label.textContent = "paused"; return; }
  if (connection === "offline") { chip.classList.add("offline"); label.textContent = "offline"; return; }
  const ageMs = lastUpdate ? Date.now() - lastUpdate : null;
  if (ageMs != null && ageMs > settings.intervalMs * 2.5) { chip.classList.add("stale"); label.textContent = "stale"; return; }
  chip.classList.add("live");
  label.textContent = lastUpdate ? `${Math.max(0, Math.round((Date.now() - lastUpdate) / 1000))}s ago` : "live";
}

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
  const cs = getComputedStyle(document.documentElement);
  const cGrid = (cs.getPropertyValue("--grid-soft").trim() || "#303238");
  const cAxis = (cs.getPropertyValue("--axis").trim() || "#a4a6ad");
  const cText = (cs.getPropertyValue("--text").trim() || "#f2f3f0");
  const w = svg.clientWidth || 800, h = svg.clientHeight || 260;
  const padL = opts.padLeft ?? 54, padR = opts.padRight ?? 18, padT = opts.padTop ?? 36, padB = opts.padBottom ?? 34;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const plotW = Math.max(1, w - padL - padR), plotH = Math.max(1, h - padT - padB);
  const cleanSeries = series.map(s => ({...s, data:(s.data || []).filter(p => Number.isFinite(Date.parse(p.t)) && Number.isFinite(num(p.y)))}));
  const points = cleanSeries.flatMap(s => s.data);
  svg.innerHTML = "";
  if (!points.length) {
    svg.innerHTML = `<text x="${w/2}" y="${h/2}" fill="${cAxis}" text-anchor="middle">No settled points yet</text>`;
    return;
  }
  const xs = points.map(p => Date.parse(p.t));
  const ys = points.map(p => num(p.y));
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  let minY = Math.min(...ys), maxY = Math.max(...ys);
  minY = Math.min(minY, 0);
  maxY = Math.max(maxY, 0);
  if (minY === maxY) { minY -= 1; maxY += 1; }
  const yPad = Math.max((maxY - minY) * .08, .5);
  minY -= yPad; maxY += yPad;
  const x = t => padL + ((Date.parse(t) - minX) / Math.max(1, maxX - minX)) * plotW;
  const y = v => padT + plotH - ((num(v) - minY) / (maxY - minY)) * plotH;
  const yTicks = niceTicks(minY, maxY, 5);
  yTicks.forEach(t => {
    const yy = y(t);
    svg.insertAdjacentHTML("beforeend", `<line x1="${padL}" x2="${w-padR}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" stroke="${cGrid}" stroke-width="1" opacity=".55"/>`);
    svg.insertAdjacentHTML("beforeend", `<text x="${padL-8}" y="${(yy+4).toFixed(1)}" fill="${cAxis}" text-anchor="end" font-size="11">${axisMoney(t)}</text>`);
  });
  const xTicks = timeTicks(minX, maxX, 4);
  xTicks.forEach(t => {
    const xx = padL + ((t - minX) / Math.max(1, maxX - minX)) * plotW;
    svg.insertAdjacentHTML("beforeend", `<text x="${xx.toFixed(1)}" y="${h-10}" fill="${cAxis}" text-anchor="middle" font-size="11">${timeTickLabel(t, minX, maxX)}</text>`);
  });
  svg.insertAdjacentHTML("beforeend", `<line x1="${padL}" x2="${padL}" y1="${padT}" y2="${h-padB}" stroke="${cGrid}" stroke-width="1"/>`);
  svg.insertAdjacentHTML("beforeend", `<line x1="${padL}" x2="${w-padR}" y1="${h-padB}" y2="${h-padB}" stroke="${cGrid}" stroke-width="1"/>`);
  const zero = y(0);
  if (zero >= padT && zero <= h - padB) {
    svg.insertAdjacentHTML("beforeend", `<line x1="${padL}" x2="${w-padR}" y1="${zero.toFixed(1)}" y2="${zero.toFixed(1)}" stroke="${cText}" stroke-width="1" opacity=".28"/>`);
  }
  cleanSeries.forEach(s => {
    const sampled = sampleLineData(s.data, opts.maxPoints ?? 180);
    const d = sampled.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join(" ");
    if (d) svg.insertAdjacentHTML("beforeend", `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>`);
  });
  const latest = cleanSeries.map(s => {
    const last = (s.data || [])[s.data.length - 1];
    return last ? `<span style="color:${s.color}">${esc(s.label)} ${money(last.y)}</span>` : "";
  }).filter(Boolean).join(" &nbsp; ");
  svg.insertAdjacentHTML("beforeend", `<foreignObject x="${padL}" y="7" width="${w-padL-padR}" height="24"><div xmlns="http://www.w3.org/1999/xhtml" style="font:12px system-ui;color:${cAxis};white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${latest}</div></foreignObject>`);
}

function niceTicks(min, max, target=5) {
  const span = Math.max(1e-9, max - min);
  const rough = span / Math.max(1, target - 1);
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  const step = [1, 2, 5, 10].map(n => n * pow).find(s => s >= rough) || 10 * pow;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * .5; v += step) ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
  return ticks.slice(0, 7);
}

function timeTicks(minX, maxX, target=4) {
  if (minX === maxX) return [minX];
  return Array.from({length:target}, (_, i) => minX + (maxX - minX) * (i / Math.max(1, target - 1)));
}

function timeTickLabel(ms, minX, maxX) {
  const opts = maxX - minX > 86400000
    ? {month:"short", day:"numeric"}
    : {hour:"numeric", minute:"2-digit"};
  return new Intl.DateTimeFormat(undefined, opts).format(new Date(ms));
}

function sampleLineData(data, maxPoints) {
  if (!data || data.length <= maxPoints) return data || [];
  const stride = Math.ceil(data.length / maxPoints);
  const sampled = data.filter((_, i) => i % stride === 0);
  const last = data[data.length - 1];
  if (sampled[sampled.length - 1] !== last) sampled.push(last);
  return sampled;
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

function postConfig(id, value) {
  return fetch("/api/config", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id, value})})
    .then(r => r.json().then(p => ({ok: r.ok, p})));
}
function controlInput(c) {
  if (c.locked) return `<span class="lock" title="${esc(c.desc)}">🔒 locked (config.yaml only)</span>`;
  if (c.type === "bool")
    return `<label class="switch"><input type="checkbox" data-id="${esc(c.id)}" ${c.value ? "checked" : ""}><span class="track"><span class="knob"></span></span></label>`;
  if (c.type === "enum")
    return `<select class="ctrl-select" data-id="${esc(c.id)}">${(c.options || []).map(o => `<option ${o === c.value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  if (c.type === "number")
    return `<input class="ctrl-num" type="number" data-id="${esc(c.id)}" value="${esc(c.value)}" ${c.min != null ? `min="${c.min}"` : ""} ${c.max != null ? `max="${c.max}"` : ""} step="${c.step ?? "any"}">`;
  return "";
}
function renderControls(d) {
  const cs = d.controls || []; if (!$("controls")) return;
  // Never rebuild while the user is mid-interaction with a toggle/select/number in this panel.
  if ($("controls").contains(document.activeElement)) return;
  const overridden = cs.filter(c => c.source === "overlay").length;
  $("controls-summary").textContent = `${cs.length} flags · ${overridden} overridden via config.local.yaml · most changes need the named worker restarted to apply`;
  const op = $("overlay-pill");
  if (op) { op.textContent = d.config_overlay_active ? "config.local overlay active" : "no overlay · canonical config"; op.classList.toggle("on", !!d.config_overlay_active); }
  const groups = ["Lanes", "Versions", "Guardrail"];
  $("controls").innerHTML = groups.map(g => {
    const rows = cs.filter(c => c.group === g);
    if (!rows.length) return "";
    return `<div class="detail-title" style="margin:4px 0 8px">${esc(g)}</div><div class="ctrl-grid">` + rows.map(c => {
      const pnl = c.pnl != null
        ? `<span class="ctrl-pnl ${valClass(c.pnl)}">${money(c.pnl)} realized (this lane)</span>`
        : (c.pnl_kind ? `<span class="ctrl-pnl muted">no realized rows yet</span>` : `<span class="ctrl-pnl muted">version — changes future rows only</span>`);
      const restart = c.restart ? `<span class="miniflag" title="Restart this worker to apply the change">↻ ${esc(c.restart)}</span>` : "";
      const cost = c.versioned_cost ? `<span class="miniflag" title="Changes every recorded cost — versioned, not silent">cost-vector</span>` : "";
      return `<div class="ctrl-row ${c.betting ? "betting" : ""}">
        <div class="ctrl-meta">
          <div class="ctrl-name">${esc(c.label)} <span class="src ${esc(c.source)}">${esc(c.source)}</span> ${restart} ${cost}</div>
          <div class="ctrl-desc">${esc(c.desc)}</div>
          <div>${pnl}</div>
        </div>
        <div>${controlInput(c)}</div>
      </div>`;
    }).join("") + `</div>`;
  }).join("");
  $("controls").querySelectorAll("[data-id]").forEach(el => {
    el.onchange = async () => {
      const id = el.dataset.id;
      const value = el.type === "checkbox" ? el.checked : el.value;
      el.disabled = true;
      try {
        const {ok, p} = await postConfig(id, value);
        if (!ok || !p.ok) { toast(p.error || "config update failed", "bad", 6000); if (state) renderControls(state); return; }
        state = p.state; lastUpdate = Date.now();
        toast(p.message || "updated", "good", 4500);
        render(state);
      } catch (err) { toast(String(err), "bad", 6000); if (state) renderControls(state); }
      finally { el.disabled = false; }
    };
  });
}

function renderAttribution(d) {
  const rows = d.attribution || []; if (!$("attribution")) return;
  if (!rows.length) { $("attribution").innerHTML = `<div class="muted" style="margin-top:10px">No settled headline rows to attribute yet.</div>`; return; }
  const max = Math.max(1, ...rows.map(r => Math.abs(num(r.pnl))));
  const sum = rows.reduce((a, r) => a + num(r.pnl), 0);
  $("attribution").innerHTML = `<div class="detail-title" style="margin:4px 0 2px">Profit attribution by strategy <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">· sums to headline ${money(sum)} (trusted/clean only)</span></div>` + rows.map(r => {
    const v = num(r.pnl), w = Math.abs(v) / max * 50;
    const fill = v >= 0
      ? `<span class="attrib-fill" style="left:50%;width:${w}%;background:var(--green)"></span>`
      : `<span class="attrib-fill" style="right:50%;width:${w}%;background:var(--red)"></span>`;
    return `<div class="attrib-row"><span class="mono">${esc(laneLabel(r.kind))}</span><span class="attrib-track">${fill}<span class="attrib-zero" style="left:50%"></span></span><span class="${valClass(v)}" style="text-align:right">${money(v)} <span class="muted">${r.n}</span></span></div>`;
  }).join("");
}

function drawDrawdown(svg, curve) {
  let peak = -Infinity;
  const dd = (curve || []).map(p => { peak = Math.max(peak, num(p.y)); return {t: p.t, y: round4(num(p.y) - peak)}; });
  drawLines(svg, [{label: "drawdown", color: colors.raw, data: dd}], {maxPoints: 200});
}
function round4(v) { return Math.round(num(v) * 1e4) / 1e4; }

function renderEquity(d) {
  const tabs = [["headline", "Headline"], ["strategy", "By strategy"], ["drawdown", "Drawdown"]];
  if ($("equity-tabs")) {
    $("equity-tabs").innerHTML = tabs.map(([m, l]) => `<button class="tab ${m === activeEquityMode ? "active" : ""}" data-mode="${m}" type="button">${esc(l)}</button>`).join("");
    $("equity-tabs").querySelectorAll("button").forEach(b => b.onclick = () => { activeEquityMode = b.dataset.mode; renderEquity(state); });
  }
  const svg = $("equity"); if (!svg) return;
  if (activeEquityMode === "strategy") {
    const byKind = d.equity_by_kind || {};
    const series = Object.keys(byKind).filter(k => (byKind[k] || []).length)
      .map(k => ({label: laneLabel(k), color: kindColor(k), data: byKind[k]}));
    if ($("equity-summary")) $("equity-summary").textContent = "cumulative clean optimistic P&L per strategy (ARB = trusted lane only)";
    drawLines(svg, series);
  } else if (activeEquityMode === "drawdown") {
    if ($("equity-summary")) $("equity-summary").textContent = "peak-to-trough of the clean optimistic curve";
    drawDrawdown(svg, (d.equity || {}).optimistic_clean);
  } else {
    if ($("equity-summary")) $("equity-summary").textContent = "raw · clean · confirmed cumulative settled P&L";
    drawLines(svg, [
      {label: "raw", color: colors.raw, data: d.equity.optimistic_raw},
      {label: "clean", color: colors.clean, data: d.equity.optimistic_clean},
      {label: "confirmed", color: colors.confirmed, data: d.equity.confirmed_clean},
    ]);
  }
  renderAttribution(d);
}

const HERO_LABELS = {
  confirmed_clean: "Clean · confirmed — the honest baseline",
  optimistic_clean: "Clean · optimistic",
  optimistic_raw: "All settled rows — incl. provisional & quarantined",
};
function seriesTotal(series) { const a = series || []; return a.length ? num(a[a.length - 1].y) : 0; }
function renderHero(d) {
  const seg = $("hero-seg"); if (!seg || !$("hero")) return;
  seg.querySelectorAll("button").forEach(b => {
    b.classList.toggle("active", b.dataset.variant === activeHeroVariant);
    b.onclick = () => { activeHeroVariant = b.dataset.variant; renderHero(state); };
  });
  const h = d.headline || {by_lane: {}, optimistic: 0, confirmed: 0, excluded_review_pnl: 0, excluded_review_n: 0, excluded_quarantined_pnl: 0, excluded_quarantined_n: 0};
  const me = d.micro_ev || {};
  const isConfirmed = activeHeroVariant === "confirmed_clean";
  const isRaw = activeHeroVariant === "optimistic_raw";
  const field = isConfirmed ? "confirmed" : "optimistic";
  // The clean/confirmed total IS the sum of the headline lane chips below it — one definition,
  // so the number always equals its parts. Raw = that plus the explicitly-excluded buckets.
  const total = isRaw ? seriesTotal((d.equity || {}).optimistic_raw) : (isConfirmed ? num(h.confirmed) : num(h.optimistic));
  const lanes = Object.entries(h.by_lane || {});
  const chips = lanes.length
    ? lanes.map(([k, v]) => `<div class="track-card"><div class="label">${esc(laneLabel(k))}</div><div class="value ${valClass(v[field])}" style="font-size:19px">${money(v[field])}</div><div class="muted">${v.n} settled${k === "ARB" ? " · trusted lane" : ""}</div></div>`).join("")
    : `<div class="muted">No settled headline rows yet.</div>`;
  // Everything intentionally NOT in the headline number, shown so nothing looks "missing".
  const excluded = [];
  if (h.excluded_review_n) excluded.push(`<span class="miniflag" title="Provisional cross-venue locks — not real money until both rulebooks confirm (hard rule #6).">REVIEW arb ${money(h.excluded_review_pnl)} · ${h.excluded_review_n} provisional</span>`);
  if (me.settled) excluded.push(`<span class="miniflag" title="Thin-book lane, kept off the headline. Shown in its own metric card.">Micro EV ${money(me.pnl)} · ${me.settled}</span>`);
  if (h.excluded_quarantined_n) excluded.push(`<span class="miniflag" title="Suspect-edge / below-floor / stale rows quarantined out of the clean book.">quarantined ${money(h.excluded_quarantined_pnl)} · ${h.excluded_quarantined_n}</span>`);
  const exclLine = excluded.length
    ? `<div class="muted" style="margin-top:10px">${isRaw ? "This number adds back:" : "Excluded from this number:"} ${excluded.join(" ")}</div>` : "";
  $("hero").innerHTML = `
    <div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:16px;margin-bottom:2px">
      <div style="font-size:46px;font-weight:800;line-height:1" class="${valClass(total)}">${money(total)}</div>
      <div class="muted">${esc(HERO_LABELS[activeHeroVariant])}${isRaw ? "" : " · = sum of the lanes below"}</div>
    </div>
    <div class="track-grid" style="margin-top:12px">${chips}</div>
    ${exclLine}`;
}

function renderMetrics(d) {
  const running = d.processes.filter(p => p.status === "ok").length;
  const unhealthy = d.processes.filter(p => !["ok","disabled"].includes(p.status));
  const enabled = d.processes.filter(p => p.status !== "disabled").length;
  const workerDetail = unhealthy.length
    ? unhealthy.map(p => `${p.status}: ${p.name}`).join(" · ")
    : `${running}/${enabled || d.processes.length} enabled workers running`;
  const ce = d.clean_ev || {pnl:0,won:0,lost:0,sold:0,settled:0,roi:0}, se = d.suspect_ev || {n:0,pnl:0}, me = d.micro_ev || {active:0,pnl:0,sold:0,settled:0,roi:0,active_stake:0}, la = d.locked_arb || {pnl:0,resolved:0,settled:0,trusted_pnl:0,trusted_settled:0,review_pnl:0,review_settled:0};
  const suspectNote = se.n ? ` · suspect: ${se.n} (${money(se.pnl)})` : "";
  const cleanSold = ce.sold ? ` · ${ce.sold} sold` : "";
  const microSold = me.sold ? ` · ${me.sold} sold` : "";
  $("metrics").innerHTML = [
    metric("Clean EV P&L", `<span class="${valClass(ce.pnl)}">${money(ce.pnl)}</span>`, `${ce.won}W / ${ce.lost}L${cleanSold} · ${ce.settled} realized · ${pct(ce.roi)} ROI${suspectNote}`),
    metric("Micro EV", `<span class="${valClass(me.pnl)}">${money(me.pnl)}</span>`, `${me.active} open · ${me.settled} realized${microSold} · ${pct(me.roi)} ROI · open stake ${usd(me.active_stake)}`, "blue"),
    metric("Locked ARB (trusted)", `<span class="${valClass(la.trusted_pnl)}">${money(la.trusted_pnl)}</span>`, `${la.trusted_settled||0} clean · REVIEW ${la.review_settled||0} = ${money(la.review_pnl)} provisional${la.invalid_in_play?` · ${la.invalid_in_play} invalid`:""}`, la.trusted_settled?"":"warn"),
    metric("Open Bets", compact(d.health.open_bets), `${compact(d.health.flat_bets)} ledger rows`, "blue"),
    metric("ARB Verification", `<span class="good">${d.arb.verified}</span><span class="muted"> / </span><span class="bad">${d.arb.vanished}</span>`, `${d.arb.unverifiable} unverifiable`),
    metric("Capital Exposure", `<span class="${(d.exposure&&d.exposure.over_bankroll)?'bad':''}">${usd((d.exposure||{}).committed||0)}</span>`,
           `of ${usd((d.exposure||{}).bankroll||0)} bankroll${(d.exposure&&d.exposure.settler_errors)?` · ${d.exposure.settler_errors} settler-error`:''}`,
           (d.exposure&&d.exposure.over_bankroll)?"bad":"blue"),
    metric("Quotes", compact(d.health.quotes), `latest ${age(d.health.last_quote_age_seconds)}`),
    metric("Worker Health", `${running}/${enabled || d.processes.length}`, workerDetail, unhealthy.length ? "bad" : "good"),
  ].join("");
}

function renderAlerts(d) {
  const a = d.alerts || [];
  if (!a.length) { $("alerts").innerHTML = ""; return; }
  $("alerts").innerHTML = a.map(x => `<div class="alert ${x.level}">${esc(x.text)}</div>`).join("");
}

function renderLiveBaseball(d) {
  const lb = d.live_baseball;
  if (!lb || !$("live-baseball")) return;
  if (!lb.total) {
    $("live-baseball").innerHTML = `<div class="muted">No live candidates recorded yet (Phase 0/1 feasibility first; betting OFF).</div>`;
    return;
  }
  const vr = lb.vanished_rate === null ? "—" : pct(lb.vanished_rate);
  const cells = [
    ["Confirmed candidates", lb.confirmed, "blue"],
    ["VANISHED", `${lb.vanished} (${vr})`, lb.vanished_rate > 0.5 ? "bad" : "warn"],
    ["Settled paper P&L", money(lb.settled_pnl), valClass(lb.settled_pnl)],
    ["Settled", lb.settled_n, "muted"],
    ["Research (<3 books)", lb.research, "muted"],
    ["Stale-rejected", lb.stale_rejected, "muted"],
    ["Suspension-rejected", lb.suspension_rejected, "muted"],
  ];
  $("live-baseball").innerHTML =
    `<div class="track-grid">` +
    cells.map(([label, val, cls]) =>
      `<div class="track-card"><div class="label">${esc(label)}</div><div class="value ${cls || ""}">${val}</div></div>`).join("") +
    `</div><div class="muted" style="margin-top:8px">VANISHED-vs-null gap, CLV at +30s/+2m/next-inning, and avg effective_state_age accrue once the Phase-1 live runner captures marks. This lane is never folded into clean pregame EV.</div>`;
}

function renderCleanEv(d) {
  const ce = d.clean_ev || {pnl:0, stake:0, roi:0, settled:0, won:0, lost:0, sold:0, win_rate:0};
  const ev = d.edge_sized_ev;
  if (!ev || !$("clean-ev-panel")) return;
  const tabs = [["flat", "Flat $1"], ["edge", "Edge-sized"]];
  $("clean-ev-tabs").innerHTML = tabs
    .map(([mode, label]) => `<button class="tab ${mode === activeCleanEvMode ? "active" : ""}" data-mode="${mode}" type="button">${esc(label)}</button>`)
    .join("");
  $("clean-ev-tabs").querySelectorAll("button").forEach(btn => {
    btn.onclick = () => { activeCleanEvMode = btn.dataset.mode; renderCleanEv(state); };
  });
  if (activeCleanEvMode === "flat") {
    const sold = ce.sold ? ` · ${ce.sold} sold` : "";
    $("clean-ev-summary").textContent = "actual clean EV paper track · flat $1 stake per row";
    $("clean-ev-panel").innerHTML = [
      deltaCard("Flat $1 P&L", money(ce.pnl), `${ce.won}W / ${ce.lost}L${sold} · ${ce.settled} realized`, valClass(ce.pnl)),
      deltaCard("Stake", usd(ce.stake), `${pct(ce.roi)} ROI · clean EV only`, "blue"),
      deltaCard("Win Rate", pct(ce.win_rate), `${ce.won + ce.lost} game outcomes`),
      deltaCard("Suspect Excluded", `${(d.suspect_ev || {}).n || 0}`, "quarantined rows not counted", "warn"),
      deltaCard("Rejected / Invalid", `${(d.rejected_ev || {}).n || 0}`, "DROP, started, final, terminal, or duplicate blocks", "warn"),
      deltaCard("Rule", "$1 / bet", "current dashboard headline sizing"),
      deltaCard("Lane", "Actionable", "clean + untagged legacy or KEEP"),
    ].join("");
    return;
  }
  const variants = ev.variants || {keep: ev, broad: null};
  const keep = variants.keep;
  const broad = variants.broad;
  if (!keep || !broad) {
    $("clean-ev-summary").textContent = `${ev.rule || "$1 per 1% displayed edge"} · waiting for comparison data`;
    $("clean-ev-panel").innerHTML = [
      deltaCard("Edge-sized", "No data", "reload once the state API includes KEEP and all-clean variants", "warn"),
    ].join("");
    return;
  }
  const keepDetail = keep.new_settled
    ? `${keep.new_won}W / ${keep.new_lost}L · max ${usd(keep.max_new_stake)}`
    : `waiting for row id > ${keep.baseline_max_id}`;
  const broadDetail = broad.new_settled
    ? `${broad.new_won}W / ${broad.new_lost}L · max ${usd(broad.max_new_stake)}`
    : `waiting for row id > ${broad.baseline_max_id}`;
  const validationGap = num(broad.new_pnl) - num(keep.new_pnl);
  const totalGap = num(broad.pnl) - num(keep.pnl);
  $("clean-ev-summary").textContent =
    `${ev.rule} · KEEP-only beside all clean EV rows`;
  $("clean-ev-panel").innerHTML = [
    deltaCard("KEEP P&L", money(keep.pnl), `${keep.settled} settled · from ${money(keep.baseline_pnl)} start`, valClass(keep.pnl)),
    deltaCard("All Clean P&L", money(broad.pnl), `${broad.settled} settled · from ${money(broad.baseline_pnl)} start`, valClass(broad.pnl)),
    deltaCard("KEEP Since", money(keep.new_pnl), keepDetail, valClass(keep.new_pnl)),
    deltaCard("All Clean Since", money(broad.new_pnl), broadDetail, valClass(broad.new_pnl)),
    deltaCard("KEEP Stake", usd(keep.stake), `${pct(keep.roi)} ROI · avg new ${usd(keep.avg_new_stake)}`, "blue"),
    deltaCard("All Clean Stake", usd(broad.stake), `${pct(broad.roi)} ROI · avg new ${usd(broad.avg_new_stake)}`, "blue"),
    deltaCard("Validation Gap", money(validationGap), `all clean minus KEEP since start · total gap ${money(totalGap)}`, valClass(validationGap)),
    deltaCard("Filters", "Clean only", `KEEP adds ${esc(keep.filter_label)} · all clean uses ${esc(broad.filter_label)}`),
  ].join("");
}

function renderProcesses(d) {
  $("clock").textContent = `live ${new Date(d.now).toLocaleTimeString()} | ${compact(d.health.polls)} polls`;
  $("procs").innerHTML = d.processes.map(p => `<span class="proc ${p.status}"><span class="dot"></span>${esc(p.name)}${p.status === "ok" ? "" : ` ${esc(p.status)}`}</span>`).join("");
}

function renderPipeline(d) {
  const max = Math.max(...Object.values(d.grades).map(num), 1);
  $("pipeline-total").textContent = `${d.arb.open_n} open ARBs`;
  const order = ["OPEN","VERIFIED","CONFIRMED","LIFETIME","SETTLED","EXITED","VANISHED","UNVERIFIABLE","REVIEW"];
  $("pipeline").innerHTML = order.filter(k => d.grades[k]).map(k => {
    const n = d.grades[k], color = k === "VANISHED" ? "var(--red)" : k === "UNVERIFIABLE" ? "var(--gold)" : k === "VERIFIED" || k === "CONFIRMED" ? "var(--green)" : "var(--blue)";
    return `<div class="bar-row"><span><span class="badge ${k}">${k}</span></span><span class="bar-track"><span class="bar-fill" style="width:${Math.max(4,n/max*100)}%;background:${color}"></span></span><b>${n}</b></div>`;
  }).join("") || `<div class="muted">No ARB rows yet</div>`;
}

function scannerStatus(scanner) {
  const label = scanner.status === "ok" ? "running"
    : scanner.status === "disabled" ? "disabled"
    : scanner.status === "stopping" ? "stopping"
    : scanner.status === "duplicate" ? `duplicate x${scanner.count}`
    : "down";
  return `<span class="status-chip ${esc(scanner.status)}"><span class="dot"></span>${esc(label)}</span>`;
}

function scannerNext(scanner) {
  const next = scanner.next;
  if (!next) return `<b>None queued</b><div class="scanner-meta">no active event in this group</div>`;
  const seconds = num(next.starts_in_seconds);
  const when = seconds < -60 ? `${duration(Math.abs(seconds))} past`
    : seconds < 60 ? "now"
    : `in ${duration(seconds)}`;
  return `<b title="${esc(next.event_key || next.event)}">${esc(next.event)}</b><div class="scanner-meta">${esc(when)} · ${esc(next.kind)} · edge ${pct(next.edge)}</div>`;
}

function scannerReturn(scanner) {
  const r = scanner.returns || {};
  if (!r.settled) return `<b class="muted">No realized data</b><div class="scanner-meta">return not proven yet</div>`;
  const roi = r.roi == null ? "n/a" : pct(r.roi);
  return `<b class="${valClass(r.pnl)}">${roi}</b><div class="scanner-meta">${money(r.pnl)} over ${r.settled} realized · avg ${money(r.avg_pnl)}</div>`;
}

function scannerLastPoll(scanner) {
  const p = scanner.last_poll;
  if (!p) return `<b class="muted">No poll row</b><div class="scanner-meta">${esc(scanner.detail)}</div>`;
  return `<b>${age(p.age_seconds)}</b><div class="scanner-meta">${esc(p.kind)} · ${p.quotes ?? 0} quotes · ${p.opps ?? 0} flags</div>`;
}

async function setScanner(scanner, action, button) {
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/scanner", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({scanner, action}),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "scanner update failed");
    state = payload.state;
    lastUpdate = Date.now();
    render(state);
    toast(`${scanner}: ${action === "stop" ? "stop requested" : (payload.message || "started")}`, "good", 3000);
  } catch (err) {
    toast(err.message || String(err), "bad", 6000);
    if (button) button.disabled = false;
  }
}

function renderScanners(d) {
  const scanners = d.scanners || [];
  const running = scanners.filter(s => s.status === "ok").length;
  const disabled = scanners.filter(s => s.status === "disabled").length;
  const controllable = scanners.filter(s => s.control !== "parent").length;
  $("scanner-summary").textContent = `${running} running · ${disabled} disabled · ${controllable} controllable workers · ${scanners.length - controllable} linked lane`;
  $("scanners").innerHTML = scanners.map(s => {
    const linked = s.control === "parent";
    const canStop = !linked && (s.status === "ok" || s.status === "duplicate" || s.status === "stopping");
    const action = canStop ? "stop" : "start";
    const buttonClass = linked ? "linked" : (canStop ? "stop" : "start");
    const buttonText = linked ? "Linked" : (canStop ? "Deactivate" : "Activate");
    return `<div class="scanner-row">
      <div><button class="scanner-toggle ${buttonClass}" data-scanner="${esc(s.id)}" data-action="${action}" type="button" ${linked ? "disabled" : ""}>${buttonText}</button></div>
      <div><div class="scanner-title">${scannerStatus(s)}<span>${esc(s.name)}</span></div><div class="scanner-meta">${esc(s.detail)}</div></div>
      <div>${scannerNext(s)}</div>
      <div>${scannerReturn(s)}</div>
      <div>${scannerLastPoll(s)}</div>
    </div>`;
  }).join("") || `<div class="muted">No scanners configured.</div>`;
  $("scanners").querySelectorAll("button:not(:disabled)").forEach(btn => {
    btn.onclick = () => setScanner(btn.dataset.scanner, btn.dataset.action, btn);
  });
}

function renderOpenBets(d) {
  const counts = (d.open_bets || []).reduce((acc, bet) => {
    acc.ALL = (acc.ALL || 0) + 1;
    acc[bet.lane || bet.kind] = (acc[bet.lane || bet.kind] || 0) + 1;
    return acc;
  }, {});
  // DIVERGENCE is never an open bet (research only); NEGRISK only appears here if executable.
  const kinds = ["EV", "MICRO_EV", "ARB", "NEGRISK", "ALL"].filter(kind => counts[kind]);
  if (!kinds.includes(activeOpenKind)) activeOpenKind = kinds.includes("EV") ? "EV" : (kinds[0] || "ALL");
  $("open-tabs").innerHTML = kinds.map(kind => `<button class="tab ${kind === activeOpenKind ? "active" : ""}" data-kind="${esc(kind)}">${esc(laneLabel(kind))} ${counts[kind]}</button>`).join("");
  $("open-tabs").querySelectorAll("button").forEach(btn => btn.onclick = () => { activeOpenKind = btn.dataset.kind; renderOpenBets(state); });
  const searchEl = $("open-search");
  if (searchEl && searchEl.oninput === null) searchEl.oninput = () => { openSearch = searchEl.value; renderOpenBets(state); };
  const byKind = (d.open_bets || []).filter(bet => activeOpenKind === "ALL" || (bet.lane || bet.kind) === activeOpenKind);
  const q = openSearch.trim().toLowerCase();
  const shown = q
    ? byKind.filter(bet => `${bet.id} ${bet.event || ""} ${bet.venue || ""} ${bet.outcome || ""} ${bet.sport || ""} ${(bet.legs || []).join(" ")}`.toLowerCase().includes(q))
    : byKind;
  const clean = shown.filter(bet => bet.quality === "clean").length;
  const hiddenNote = d.negrisk_hidden ? ` · ${d.negrisk_hidden} NEGRISK not executable (hidden)` : "";
  const filterNote = q ? ` · filtered by “${esc(q)}” (${shown.length}/${byKind.length})` : "";
  $("open-summary").innerHTML = `${shown.length} shown · ${clean} clean · grouped by event date${hiddenNote}${filterNote}`;
  const order = ["overdue", "today", "tomorrow", "week", "later", "unknown"];
  const labels = {overdue:"Awaiting settlement", today:"Today", tomorrow:"Tomorrow", week:"2-7 days", later:"8+ days", unknown:"No event date"};
  const groups = order
    .map(key => [key, shown.filter(bet => bet.horizon === key)])
    .filter(([, bets]) => bets.length);
  if (!groups.length) {
    $("open-bets").innerHTML = q
      ? `<div class="muted">No open ${esc(laneLabel(activeOpenKind))} bets match “${esc(q)}”.</div>`
      : `<div class="muted">No open ${esc(laneLabel(activeOpenKind))} bets right now.</div>`;
    return;
  }
  $("open-bets").innerHTML = groups.map(([key, bets]) => {
    const rows = bets.slice(0, 60).map(bet => {
      const detail = [
        bet.outcome ? `${bet.outcome}` : "",
        bet.venue ? `@${bet.venue}` : "",
        bet.cost ? `cost ${bet.cost}` : "",
        bet.stake_usd && bet.quality === "micro" ? `stake $${bet.stake_usd}` : "",
        bet.depth_usd ? `depth $${bet.depth_usd}` : "",
        bet.arb_sizing ? `model stake ${usd(bet.arb_sizing.stake)}` : "",
      ].filter(Boolean).join(" ");
      const sizingReason = bet.arb_sizing ? `<div class="subline">${esc(bet.arb_sizing.reason)}</div>` : "";
      const flags = [
        bet.quality !== "clean" ? `<span class="badge ${esc(bet.quality)}">${esc(bet.quality)}</span>` : "",
        bet.confirmed ? `<span class="badge CONFIRMED">CONF</span>` : "",
        bet.verified ? `<span class="badge VERIFIED">V${bet.verified}</span>` : "",
        bet.vanished ? `<span class="badge VANISHED">X${bet.vanished}</span>` : "",
        bet.live ? `<span class="badge REVIEW">LIVE</span>` : "",
      ].filter(Boolean).join(" ");
      return `<tr>
        <td class="muted mono">${bet.id}</td>
        <td><span class="badge ${esc(bet.lane || bet.kind)}">${esc(bet.display_kind || bet.kind)}</span></td>
        <td><div class="open-event" title="${esc(bet.event_key || bet.event)}">${esc(bet.event)}</div><div class="subline">${esc(detail || bet.legs.join(", "))}</div>${sizingReason}</td>
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

function renderArbSizing(d) {
  const model = d.arb_sizing_model || {};
  const projection = d.arb_scale_projection || {};
  const training = model.training || {n:0, stake:0, pnl:0, roi:0};
  if (!$("arb-sizing-cards")) return;
  const tabs = [["model", "Current Model"], ["whatif", "What-if Scale"]];
  $("arb-sizing-tabs").innerHTML = tabs
    .map(([mode, label]) => `<button class="tab ${mode === activeArbSizingMode ? "active" : ""}" data-mode="${mode}" type="button">${esc(label)}</button>`)
    .join("");
  $("arb-sizing-tabs").querySelectorAll("button").forEach(btn => {
    btn.onclick = () => { activeArbSizingMode = btn.dataset.mode; renderArbSizing(state); };
  });
  if (activeArbSizingMode === "whatif") {
    renderArbScaleProjection(projection);
    return;
  }
  const openArbs = (d.open_bets || []).filter(bet => bet.kind === "ARB");
  const scalable = openArbs.filter(bet => bet.arb_sizing && bet.arb_sizing.scalable).length;
  const totalRecommended = openArbs.reduce((sum, bet) => sum + num((bet.arb_sizing || {}).stake), 0);
  $("arb-sizing-summary").textContent =
    `${training.n || 0} settled bet-on ARBs · ${usd(training.stake || 0)} historically staked · ${money(training.pnl || 0)} P&L`;
  $("arb-sizing-cards").innerHTML = [
    deltaCard("Training Bets", compact(training.n || 0), `${usd(training.stake || 0)} actually bet`),
    deltaCard("Training P&L", money(training.pnl || 0), `${pct(training.roi || 0)} realized ROI`, valClass(training.pnl || 0)),
    deltaCard("Max Per ARB", usd(model.max_stake || 0), "bankroll cap before depth/edge caps", "blue"),
    deltaCard("Open ARBs", compact(openArbs.length), `${scalable} scalable · ${usd(totalRecommended)} recommended`, scalable ? "good" : "warn"),
  ].join("");
  const statRows = stats => Object.entries(stats || {}).map(([bucket, row]) => {
    const pnl = num(row.pnl), stake = num(row.stake), roi = num(row.roi);
    return `<tr><td><b>${esc(bucket)}</b></td><td>${row.n || 0}</td><td>${usd(stake)}</td><td class="${valClass(pnl)}">${money(pnl)}</td><td class="${valClass(roi)}">${pct(roi)}</td></tr>`;
  });
  const currentRows = openArbs.map(bet => {
    const rec = bet.arb_sizing || {};
    return `<tr>
      <td class="muted mono">${bet.id}</td>
      <td><b>${esc(bet.event)}</b><div class="subline">${esc(bet.outcome || "")} ${bet.venue ? `@${esc(bet.venue)}` : ""}</div></td>
      <td>${usd(rec.binding_depth || 0)}</td>
      <td>${rec.edge_points == null ? "" : pct(rec.edge_points)}</td>
      <td class="${rec.scalable ? "good" : "warn"}">${usd(rec.stake || 1)}</td>
      <td class="muted">${esc(rec.reason || "")}</td>
    </tr>`;
  });
  $("arb-sizing-detail").innerHTML = `
    <div class="detail-grid">
      <div class="detail-block"><div class="detail-title">Venue Pair</div>${table(["Pair","Bets","Bet","P&L","ROI"], statRows(model.venue_pair))}</div>
      <div class="detail-block"><div class="detail-title">Market Size</div>${table(["Depth","Bets","Bet","P&L","ROI"], statRows(model.depth))}</div>
      <div class="detail-block"><div class="detail-title">Edge Size</div>${table(["Edge","Bets","Bet","P&L","ROI"], statRows(model.edge))}</div>
      <div class="detail-block"><div class="detail-title">Current ARBs</div>${table(["ID","Event","Depth","Edge","Stake","Reason"], currentRows, "No open ARBs right now")}</div>
    </div>`;
}

function renderArbScaleProjection(projection) {
  const total = projection.total || {n:0, scaled_n:0, actual_stake:0, actual_pnl:0, actual_roi:0, flat10_stake:0, flat10_pnl:0, flat10_roi:0, flat10_lift:0, depth_stake:0, depth_pnl:0, depth_roi:0, depth_lift:0};
  $("arb-sizing-summary").textContent =
    `hypothetical · ${total.n || 0} settled bet-on ARBs · ${total.scaled_n || 0} had enough recorded depth to scale`;
  $("arb-sizing-cards").innerHTML = [
    deltaCard("Actual $ Bet", usd(total.actual_stake || 0), `${money(total.actual_pnl || 0)} P&L · ${pct(total.actual_roi || 0)} ROI`),
    deltaCard("$10 Where Possible", money(total.flat10_pnl || 0), `${usd(total.flat10_stake || 0)} stake · lift ${money(total.flat10_lift || 0)}`, valClass(total.flat10_pnl || 0)),
    deltaCard("Depth-Capped", money(total.depth_pnl || 0), `${usd(total.depth_stake || 0)} stake · lift ${money(total.depth_lift || 0)}`, valClass(total.depth_pnl || 0)),
    deltaCard("Rows Scaled", compact(total.scaled_n || 0), `${projection.no_depth_n || 0} rows had no complete depth`, total.scaled_n ? "good" : "warn"),
    deltaCard("Largest $10 Stake", usd(total.max_flat10_stake || 0), `target ${usd(projection.target_stake || 10)} when depth allowed`, "blue"),
    deltaCard("Largest Depth Stake", usd(total.max_depth_stake || 0), `cap ${usd(projection.max_stake || 0)} · ${(num(projection.market_depth_fraction || 0) * 100).toFixed(2)}% of depth`, "blue"),
  ].join("");
  const scaleRows = stats => Object.entries(stats || {}).map(([bucket, row]) => {
    const actual = num(row.actual_pnl), flat = num(row.flat10_pnl), depth = num(row.depth_pnl);
    return `<tr>
      <td><b>${esc(bucket)}</b></td>
      <td>${row.n || 0}</td>
      <td>${row.scaled_n || 0}</td>
      <td>${usd(row.actual_stake || 0)}</td>
      <td class="${valClass(actual)}">${money(actual)}</td>
      <td>${usd(row.flat10_stake || 0)}</td>
      <td class="${valClass(flat)}">${money(flat)}</td>
      <td>${usd(row.depth_stake || 0)}</td>
      <td class="${valClass(depth)}">${money(depth)}</td>
    </tr>`;
  });
  const exampleRows = (projection.examples || []).slice(0, 12).map(row => `
    <tr>
      <td class="muted mono">${row.id}</td>
      <td><b title="${esc(row.event)}">${esc(row.event)}</b><div class="subline">${esc(row.pair)} · ${esc(row.depth_bucket)} · ${esc(row.edge_bucket)}</div></td>
      <td>${usd(row.binding_depth || 0)}</td>
      <td>${row.edge_points == null ? "" : pct(row.edge_points)}</td>
      <td>${usd(row.actual_stake || 0)}<div class="subline">${money(row.actual_pnl || 0)}</div></td>
      <td>${usd(row.flat10_stake || 0)}<div class="subline">${money(row.flat10_pnl || 0)}</div></td>
      <td>${usd(row.depth_stake || 0)}<div class="subline ${valClass(row.depth_lift || 0)}">${money(row.depth_lift || 0)} lift</div></td>
    </tr>`);
  $("arb-sizing-detail").innerHTML = `
    <div class="muted" style="margin-bottom:10px">${esc(projection.rule || "Hypothetical only; actual P&L is unchanged.")}</div>
    <div class="detail-grid">
      <div class="detail-block"><div class="detail-title">By Venue Pair</div>${table(["Pair","Bets","Scaled","Actual Bet","Actual P&L","$10 Bet","$10 P&L","Depth Bet","Depth P&L"], scaleRows(projection.venue_pair))}</div>
      <div class="detail-block"><div class="detail-title">By Market Size</div>${table(["Depth","Bets","Scaled","Actual Bet","Actual P&L","$10 Bet","$10 P&L","Depth Bet","Depth P&L"], scaleRows(projection.depth))}</div>
      <div class="detail-block"><div class="detail-title">By Edge Size</div>${table(["Edge","Bets","Scaled","Actual Bet","Actual P&L","$10 Bet","$10 P&L","Depth Bet","Depth P&L"], scaleRows(projection.edge))}</div>
      <div class="detail-block"><div class="detail-title">Largest Scalable Historical ARBs</div>${table(["ID","Event","Depth","Edge","Actual","$10","Depth"], exampleRows, "No settled ARBs had enough recorded depth to scale.")}</div>
    </div>`;
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
  const evMicroNow = current.quality["EV:micro"] || {};
  const evMicroBase = base.quality["EV:micro"] || {};
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
    deltaCard("Micro EV", `+${delta(evMicroNow, evMicroBase, "n")}`, `${money(delta(evMicroNow, evMicroBase, "pnl"))} tiny-size paper`, valClass(delta(evMicroNow, evMicroBase, "pnl"))),
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
    ["EV micro", evMicroNow, evMicroBase],
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
  const rows = (d.quality || []).map(q => {
    const label = q.kind === "EV"
      ? (q.quality === "clean" ? "EV clean" : q.quality === "micro" ? "Micro EV" : `EV ${q.quality}`)
      : (q.quality === "clean" ? q.kind : `${q.kind} ${q.quality}`);
    const pnl = num(q.pnl);
    const edge = q.kind === "EV" && q.quality === "suspect-edge"
      ? `<span class="muted">quarantined</span>`
      : (q.avg_edge == null ? "" : pct(q.avg_edge));
    return `<tr><td><b>${esc(label)}</b></td><td>${q.n}</td><td>${q.active}</td><td>${q.settled}</td><td class="${valClass(pnl)}">${money(pnl)}</td><td>${edge}</td></tr>`;
  });
  $("kinds").innerHTML = table(["Lane","Rows","Active","Settled","P&L","Avg Edge"], rows);
  const clv = d.ev.clv || {};
  $("clv").textContent = clv.n ? `CLV n=${clv.n}, mean ${num(clv.mean_clv).toFixed(4)}` : "CLV gathering";
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

function captureScroll() {
  const doc = document.documentElement;
  const pageMax = Math.max(0, doc.scrollHeight - window.innerHeight);
  const inner = {};
  ["scanners", "open-bets", "kinds", "recent", "log"].forEach(id => {
    const el = $(id);
    if (el) inner[id] = {top: el.scrollTop, bottom: el.scrollHeight - el.clientHeight - el.scrollTop};
  });
  return {top: window.scrollY, bottom: pageMax - window.scrollY, inner};
}

function restoreScroll(snapshot) {
  if (!snapshot) return;
  requestAnimationFrame(() => {
    const doc = document.documentElement;
    const pageMax = Math.max(0, doc.scrollHeight - window.innerHeight);
    const pageTarget = snapshot.bottom < 48 ? pageMax : Math.min(snapshot.top, pageMax);
    if (Math.abs(window.scrollY - pageTarget) > 2) window.scrollTo(0, pageTarget);
    Object.entries(snapshot.inner || {}).forEach(([id, pos]) => {
      const el = $(id);
      if (!el) return;
      const max = Math.max(0, el.scrollHeight - el.clientHeight);
      el.scrollTop = pos.bottom < 24 ? max : Math.min(pos.top, max);
    });
  });
}

function render(d) {
  const scroll = captureScroll();
  renderProcesses(d);
  renderAlerts(d);
  renderLiveBaseball(d);
  renderHero(d);
  renderMetrics(d);
  renderTracking(d);
  renderCleanEv(d);
  renderScanners(d);
  renderControls(d);
  renderOpenBets(d);
  renderArbSizing(d);
  renderPipeline(d);
  renderKinds(d);
  renderRecent(d);
  renderLogs(d);
  renderEquity(d);
  restoreScroll(scroll);
}

$("reset-track").onclick = () => resetTracking(state);

function scheduleNext(delay) {
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
  if (settings.paused) return;
  refreshTimer = setTimeout(tick, delay == null ? settings.intervalMs : delay);
}

let firstLoad = true;
async function tick() {
  try {
    const response = await fetch("/api/state", {cache:"no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const next = await response.json();
    const wasOffline = connection === "offline";
    state = next;
    connection = "live";
    lastUpdate = Date.now();
    render(state);
    if (firstLoad) { firstLoad = false; decoratePanels(); buildNav(); }
    if (wasOffline) toast("Reconnected to dashboard server", "good", 3500);
  } catch (err) {
    if (connection !== "offline") toast("Lost connection to dashboard server — retrying", "bad", 4000);
    connection = "offline";
  } finally {
    updateRefreshChip();
    scheduleNext();
  }
}

// --- control wiring ---
$("refresh-toggle").onclick = () => setPaused(!settings.paused);
$("interval-seg").querySelectorAll("button").forEach(b => b.onclick = () => {
  settings.intervalMs = Number(b.dataset.int); saveSettings(); applySettings();
  if (!settings.paused) scheduleNext(0);
  toast(`Auto-refresh every ${settings.intervalMs / 1000}s`, "good", 2000);
});
$("nav-btn").onclick = () => { buildNav(); openOverlay("nav-overlay"); };
$("help-btn").onclick = () => openOverlay("help-overlay");
$("theme-btn").onclick = () => {
  settings.theme = THEMES[(THEMES.indexOf(settings.theme) + 1) % THEMES.length];
  saveSettings(); applySettings(); toast(`Theme: ${settings.theme}`, "good", 1800);
  if (state) render(state);
};
$("density-btn").onclick = () => {
  settings.density = settings.density === "compact" ? "comfortable" : "compact";
  saveSettings(); applySettings(); toast(`Density: ${settings.density}`, "good", 1800);
};
document.querySelectorAll(".overlay").forEach(o => {
  o.addEventListener("click", e => { if (e.target === o) closeOverlays(); });
  o.querySelectorAll("[data-close]").forEach(b => b.onclick = closeOverlays);
});
document.addEventListener("keydown", e => {
  if (e.target.matches("input, textarea, select") && e.key !== "Escape") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  switch (e.key) {
    case " ": e.preventDefault(); setPaused(!settings.paused); break;
    case "r": tick(); toast("Refreshed", "good", 1200); break;
    case "g": buildNav(); openOverlay("nav-overlay"); break;
    case "t": $("theme-btn").click(); break;
    case "d": $("density-btn").click(); break;
    case "?": openOverlay("help-overlay"); break;
    case "/": { e.preventDefault(); const s = $("open-search"); if (s) s.focus(); break; }
    case "Escape": closeOverlays(); break;
  }
});
// keep the freshness chip honest even between polls
setInterval(updateRefreshChip, 1000);

applySettings();
tick();
window.addEventListener("resize", () => state && render(state));
</script>
</body>
</html>"""
