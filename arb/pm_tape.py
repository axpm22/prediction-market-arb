"""Polymarket WebSocket book tape -> its own SQLite file.

Rule 1: public, unauthenticated, read-only market data. Nothing here places
        an order; the maker lane's "fills" are simulated counterfactuals.
Rule 3: the endpoint and every frame shape below were verified live
        2026-07-30 and dated in data/verified_facts.md BEFORE this module was
        written. Three event types exist: `book`, `price_change`, and
        `last_trade_price` — the last being a REAL trade print, which is what
        lets the maker lane tell a fill from a cancellation.
Rule 9: writes to data/pm_tape.sqlite, so flat_bets is structurally
        unreachable from this lane — the defence arb/kalshi_history.py uses.

This module is deliberately dumb: it records frames and does not interpret
them. All judgement lives in arb/maker.py, which never touches a socket.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from arb.logutil import get_logger

log = get_logger("arb.pm_tape", "logs/scan.log")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tape_frames (
    id INTEGER PRIMARY KEY,
    received_at REAL,        -- OUR clock: the venue timestamp cannot be trusted
                             -- for latency work and is kept separately
    venue_ts TEXT,
    event_type TEXT,
    asset_id TEXT,
    raw_json TEXT);
CREATE INDEX IF NOT EXISTS idx_tape_asset ON tape_frames(asset_id, received_at);
CREATE INDEX IF NOT EXISTS idx_tape_kind ON tape_frames(event_type, received_at);
"""


def open_tape(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    con.commit()
    return con


def _asset_of(frame: dict) -> str | None:
    """price_change frames carry NO root asset_id — it lives on each entry of
    price_changes[]. Reading the root yields None and the replay can no longer
    key the frame to a book (this exact mistake parsed 4 of 935 updates)."""
    if frame.get("asset_id"):
        return str(frame["asset_id"])
    for ch in frame.get("price_changes") or []:
        if ch.get("asset_id"):
            return str(ch["asset_id"])
    return None


def persist_frames(con, frames: list[dict], received_at: list[float]) -> int:
    rows = [(rt, str(f.get("timestamp") or ""), str(f.get("event_type") or ""),
             _asset_of(f), json.dumps(f))
            for f, rt in zip(frames, received_at)]
    con.executemany(
        "INSERT INTO tape_frames (received_at, venue_ts, event_type, asset_id,"
        " raw_json) VALUES (?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def contested_tokens(cfg) -> list[str]:
    """Liquid markets whose price is not already resolved. A book at
    0.002/0.998 cannot produce a maker fill, so recording it is dead weight."""
    from arb.venues.base import get_json
    lo = float(cfg["maker"]["min_price"])
    hi = float(cfg["maker"]["max_price"])
    cap = int(cfg["maker"]["max_tokens"])
    ev = get_json("https://gamma-api.polymarket.com/markets?closed=false"
                  "&order=volume24hr&ascending=false&limit=200", cfg)
    out: list[str] = []
    for m in ev:
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except (TypeError, ValueError):
            continue
        if len(toks) == 2 and len(prices) == 2 and lo <= prices[0] <= hi:
            out += [str(t) for t in toks]
        if len(out) >= cap:
            break
    return out[:cap]


def record(cfg, tokens: list[str], secs: int) -> dict:
    """Live recorder. `websockets` is an OPTIONAL extra — imported here, never
    at module top level, so core imports keep working without it."""
    import asyncio
    import ssl

    try:
        import certifi
        import websockets
    except ImportError as e:
        raise RuntimeError(
            "the maker tape needs the optional websockets package: "
            ".venv/bin/pip install websockets") from e

    con = open_tape(cfg["maker"]["tape_db"])
    ctx = ssl.create_default_context(cafile=certifi.where())
    stats = {"frames": 0, "batches": 0, "trades": 0}

    async def main():
        async with websockets.connect(WS_URL, ssl=ctx, open_timeout=20,
                                      max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
            end = time.time() + secs
            buf, times = [], []
            while time.time() < end:
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    break
                except Exception as e:  # noqa: BLE001 — a drop is data, not a crash
                    log.warning("pm tape socket closed: %s", type(e).__name__)
                    break
                now = time.time()
                data = json.loads(msg)
                for f in (data if isinstance(data, list) else [data]):
                    buf.append(f)
                    times.append(now)
                    if f.get("event_type") == "last_trade_price":
                        stats["trades"] += 1
                if len(buf) >= 500:      # batch: one commit per frame is slower
                    stats["frames"] += persist_frames(con, buf, times)
                    stats["batches"] += 1
                    buf, times = [], []
            if buf:
                stats["frames"] += persist_frames(con, buf, times)
                stats["batches"] += 1

    asyncio.run(main())
    stats["tokens"] = len(tokens)
    log.info("pm tape: %s", stats)
    return stats
