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

from arb.arb_sizing import build_arb_sizing_model, recommend_arb_stake
from arb.config import load_config
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
        locked_arb = c.execute(
            """
            SELECT COUNT(*) settled,
                   SUM(CASE WHEN result='RESOLVED' THEN 1 ELSE 0 END) resolved,
                   COALESCE(SUM(CAST(pnl_optimistic AS REAL)),0) pnl,
                   COALESCE(SUM(CAST(stake_usd AS REAL)),0) stake
              FROM flat_bets
             WHERE kind='ARB' AND status='SETTLED'
            """
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
                SELECT placed_at, pnl_optimistic, pnl_confirmed, quality_flag
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
    exp = compute_exposure(store, config.bankroll_usd, per_venue_cap)
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
        "locked_arb": {
            "settled": int(locked_arb["settled"] or 0),
            "resolved": int(locked_arb["resolved"] or 0),
            "pnl": _money(locked_arb["pnl"]),
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

    def do_POST(self):  # noqa: N802
        if self.path != "/api/scanner":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length) or b"{}")
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
  --bg:#111315;--panel:#1a1d21;--panel-2:#22262b;--panel-3:#15181c;--line:#343a42;--line-soft:#282d34;
  --text:#f1f3f0;--muted:#a9afb7;--muted-2:#808892;
  --green:#61c88a;--red:#ff7a70;--gold:#e5b85a;--blue:#76aefc;--violet:#bca2ff;--cyan:#6fd5ce;
  --gap:14px;--radius:8px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.48 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow-anchor:none}
button{font:inherit}
.top{position:sticky;top:0;z-index:5;background:rgba(17,19,21,.96);border-bottom:1px solid var(--line-soft);backdrop-filter:blur(10px)}
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
.panel{background:var(--panel);border:1px solid var(--line-soft);border-radius:var(--radius);min-width:0;box-shadow:0 1px 0 rgba(255,255,255,.03);overflow-anchor:none}
.panels{margin-top:var(--gap);align-items:start}
.panels>.panel{container-type:inline-size}
.metric{grid-column:span 3;padding:14px 15px;min-height:112px;display:flex;flex-direction:column;justify-content:space-between}
.wide{grid-column:span 8}.side{grid-column:span 4}.half{grid-column:span 6}.full{grid-column:span 12}
.label{color:var(--muted-2);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:7px}
.value{font-size:26px;line-height:1.08;font-weight:760;letter-spacing:0;white-space:nowrap}
.unit{font-size:12px;color:var(--muted);font-weight:520;margin-left:5px}
.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--gold)}.blue{color:var(--blue)}.muted{color:var(--muted)}
#alerts{display:flex;flex-direction:column;gap:6px;margin-bottom:12px}
.alert{padding:8px 12px;border-radius:8px;font-size:13px;font-weight:500;border:1px solid}
.alert.bad{background:rgba(229,72,77,.12);border-color:rgba(229,72,77,.4);color:var(--red)}
.alert.warn{background:rgba(214,158,46,.12);border-color:rgba(214,158,46,.35);color:var(--gold)}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:54px;padding:12px 14px;border-bottom:1px solid var(--line-soft)}
.panel-title{font-size:13px;font-weight:700;letter-spacing:0}
.panel-body{padding:14px}
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
  <div id="alerts"></div>
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
  const w = svg.clientWidth || 800, h = svg.clientHeight || 260;
  const padL = opts.padLeft ?? 54, padR = opts.padRight ?? 18, padT = opts.padTop ?? 36, padB = opts.padBottom ?? 34;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const plotW = Math.max(1, w - padL - padR), plotH = Math.max(1, h - padT - padB);
  const cleanSeries = series.map(s => ({...s, data:(s.data || []).filter(p => Number.isFinite(Date.parse(p.t)) && Number.isFinite(num(p.y)))}));
  const points = cleanSeries.flatMap(s => s.data);
  svg.innerHTML = "";
  if (!points.length) {
    svg.innerHTML = `<text x="${w/2}" y="${h/2}" fill="#a4a6ad" text-anchor="middle">No settled points yet</text>`;
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
    svg.insertAdjacentHTML("beforeend", `<line x1="${padL}" x2="${w-padR}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" stroke="#303238" stroke-width="1" opacity=".55"/>`);
    svg.insertAdjacentHTML("beforeend", `<text x="${padL-8}" y="${(yy+4).toFixed(1)}" fill="#a4a6ad" text-anchor="end" font-size="11">${axisMoney(t)}</text>`);
  });
  const xTicks = timeTicks(minX, maxX, 4);
  xTicks.forEach(t => {
    const xx = padL + ((t - minX) / Math.max(1, maxX - minX)) * plotW;
    svg.insertAdjacentHTML("beforeend", `<text x="${xx.toFixed(1)}" y="${h-10}" fill="#a4a6ad" text-anchor="middle" font-size="11">${timeTickLabel(t, minX, maxX)}</text>`);
  });
  svg.insertAdjacentHTML("beforeend", `<line x1="${padL}" x2="${padL}" y1="${padT}" y2="${h-padB}" stroke="#303238" stroke-width="1"/>`);
  svg.insertAdjacentHTML("beforeend", `<line x1="${padL}" x2="${w-padR}" y1="${h-padB}" y2="${h-padB}" stroke="#303238" stroke-width="1"/>`);
  const zero = y(0);
  if (zero >= padT && zero <= h - padB) {
    svg.insertAdjacentHTML("beforeend", `<line x1="${padL}" x2="${w-padR}" y1="${zero.toFixed(1)}" y2="${zero.toFixed(1)}" stroke="#f2f3f0" stroke-width="1" opacity=".28"/>`);
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
  svg.insertAdjacentHTML("beforeend", `<foreignObject x="${padL}" y="7" width="${w-padL-padR}" height="24"><div xmlns="http://www.w3.org/1999/xhtml" style="font:12px system-ui;color:#a4a6ad;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${latest}</div></foreignObject>`);
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

function renderMetrics(d) {
  const running = d.processes.filter(p => p.status === "ok").length;
  const unhealthy = d.processes.filter(p => !["ok","disabled"].includes(p.status));
  const enabled = d.processes.filter(p => p.status !== "disabled").length;
  const workerDetail = unhealthy.length
    ? unhealthy.map(p => `${p.status}: ${p.name}`).join(" · ")
    : `${running}/${enabled || d.processes.length} enabled workers running`;
  const ce = d.clean_ev || {pnl:0,won:0,lost:0,sold:0,settled:0,roi:0}, se = d.suspect_ev || {n:0,pnl:0}, me = d.micro_ev || {active:0,pnl:0,sold:0,settled:0,roi:0,active_stake:0}, la = d.locked_arb || {pnl:0,resolved:0,settled:0};
  const suspectNote = se.n ? ` · suspect: ${se.n} (${money(se.pnl)})` : "";
  const cleanSold = ce.sold ? ` · ${ce.sold} sold` : "";
  const microSold = me.sold ? ` · ${me.sold} sold` : "";
  $("metrics").innerHTML = [
    metric("Clean EV P&L", `<span class="${valClass(ce.pnl)}">${money(ce.pnl)}</span>`, `${ce.won}W / ${ce.lost}L${cleanSold} · ${ce.settled} realized · ${pct(ce.roi)} ROI${suspectNote}`),
    metric("Micro EV", `<span class="${valClass(me.pnl)}">${money(me.pnl)}</span>`, `${me.active} open · ${me.settled} realized${microSold} · ${pct(me.roi)} ROI · open stake ${usd(me.active_stake)}`, "blue"),
    metric("Locked ARB P&L", `<span class="${valClass(la.pnl)}">${money(la.pnl)}</span>`, `${la.settled} settled (${la.resolved} resolved) · provisional`),
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
      deltaCard("Rule", "$1 / bet", "current dashboard headline sizing"),
      deltaCard("Lane", "Clean", "quality_flag is NULL"),
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
    render(state);
  } catch (err) {
    alert(err.message || String(err));
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
  const shown = (d.open_bets || []).filter(bet => activeOpenKind === "ALL" || (bet.lane || bet.kind) === activeOpenKind);
  const clean = shown.filter(bet => bet.quality === "clean").length;
  const hiddenNote = d.negrisk_hidden ? ` · ${d.negrisk_hidden} NEGRISK not executable (hidden)` : "";
  $("open-summary").textContent = `${shown.length} shown · ${clean} clean · grouped by event date${hiddenNote}`;
  const order = ["overdue", "today", "tomorrow", "week", "later", "unknown"];
  const labels = {overdue:"Awaiting settlement", today:"Today", tomorrow:"Tomorrow", week:"2-7 days", later:"8+ days", unknown:"No event date"};
  const groups = order
    .map(key => [key, shown.filter(bet => bet.horizon === key)])
    .filter(([, bets]) => bets.length);
  if (!groups.length) {
    $("open-bets").innerHTML = `<div class="muted">No open ${esc(laneLabel(activeOpenKind))} bets right now.</div>`;
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
      ].filter(Boolean).join(" ");
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
  renderMetrics(d);
  renderTracking(d);
  renderCleanEv(d);
  renderScanners(d);
  renderOpenBets(d);
  renderPipeline(d);
  renderKinds(d);
  renderRecent(d);
  renderLogs(d);
  drawLines($("equity"), [
    {label:"raw", color:colors.raw, data:d.equity.optimistic_raw},
    {label:"clean", color:colors.clean, data:d.equity.optimistic_clean},
    {label:"confirmed", color:colors.confirmed, data:d.equity.confirmed_clean},
  ]);
  restoreScroll(scroll);
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
