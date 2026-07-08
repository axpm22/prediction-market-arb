"""L3: shortlist builder, identity re-pricing targets, migration, and the
no-Odds-API grep guarantee for the live path."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arb.config import load
from arb.lanes.live import identities, shortlist
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")
NOW = datetime(2026, 7, 9, 1, 0, tzinfo=timezone.utc)


def _quote_row(event_key, sport, start, vmid, fetched=None):
    return {"poll_id": 1, "fetched_at": (fetched or NOW).isoformat(),
            "event_key": event_key, "category": "sports", "sport": sport,
            "venue": "kalshi", "book": "kalshi", "market_type": "WINNER",
            "line": None, "outcome": "mets", "side": "YES", "raw_price": "0.5",
            "cost": "0.5", "fee_cost": "0", "depth_usd": "100", "ladder_json": None,
            "live": 1, "bid": "0.48", "volume_24h": "5",
            "start_utc": start.isoformat(), "start_grade": "nominal",
            "venue_market_id": vmid}


def test_migration_adds_venue_market_id_and_leadlag_runs(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(quotes)")}
    assert "venue_market_id" in cols
    tables = {r["name"] for r in store.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "leadlag_runs" in tables


def test_shortlist_window_cap_and_priority(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    rows = [
        _quote_row("baseball_mlb:2026-07-08:a:b", "baseball_mlb",
                   NOW - timedelta(hours=1), "T1"),          # in window
        _quote_row("baseball_mlb:2026-07-08:c:d", "baseball_mlb",
                   NOW - timedelta(hours=2), "T2"),          # in window, older
        _quote_row("baseball_mlb:2026-07-08:e:f", "baseball_mlb",
                   NOW - timedelta(hours=8), "T3"),          # past the 5h cap: out
        _quote_row("baseball_mlb:2026-07-09:g:h", "baseball_mlb",
                   NOW + timedelta(hours=2), "T4"),          # pregame: out
    ]
    store.insert_many("quotes", rows)
    events = shortlist(CFG, store, NOW)
    keys = [e["event_key"] for e in events]
    assert keys == ["baseball_mlb:2026-07-08:a:b", "baseball_mlb:2026-07-08:c:d"]
    # an open live bet promotes its event to the front
    store.insert("flat_bets", {
        "created_at": NOW.isoformat(), "poll_id": 1, "poll_kind": "live",
        "fingerprint": "f", "position_key": "p", "kind": "XSPREAD",
        "category": "sports", "sport": "baseball_mlb", "venue_pair": "kalshi:sx",
        "event_key": "baseball_mlb:2026-07-08:c:d",
        "event_start_utc": (NOW - timedelta(hours=2)).isoformat(),
        "start_grade": "nominal", "legs_json": "[]", "stake_usd": "1",
        "edge_points": "0.01", "trusted": 0, "lane": "review",
        "quality_flag": "none", "leg_skew_secs": 1, "status": "REVIEW",
        "pnl_optimistic": "0.01", "notes": ""})
    events = shortlist(CFG, store, NOW)
    assert events[0]["event_key"] == "baseball_mlb:2026-07-08:c:d"


def test_identities_latest_per_key(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.insert_many("quotes", [
        _quote_row("baseball_mlb:2026-07-08:a:b", "baseball_mlb",
                   NOW - timedelta(hours=1), "TICK-1"),
        _quote_row("baseball_mlb:2026-07-08:a:b", "baseball_mlb",
                   NOW - timedelta(hours=1), "TICK-1",
                   fetched=NOW - timedelta(minutes=5))])
    idents = identities(store, ["baseball_mlb:2026-07-08:a:b"])
    assert len(idents) == 1
    assert idents[0]["venue_market_id"] == "TICK-1"


def test_live_path_never_touches_odds_api():
    text = (ROOT / "arb" / "lanes" / "live.py").read_text()
    assert not re.search(r"oddsapi|the-odds-api", text, re.I)


def test_live_lane_disabled_is_noop(tmp_path, capsys):
    from arb.commands import cmd_live

    class A:
        dry_run = True
        once = True
    cfg = load(ROOT / "config.yaml")
    cfg["live_lane"]["enabled"] = False
    assert cmd_live(cfg, Store(tmp_path / "t.sqlite"), A()) == 0
    assert "no-op" in capsys.readouterr().out
