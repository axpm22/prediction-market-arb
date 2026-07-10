"""LIVE lane (prompts/PROMPT_LIVE_LEADLAG.md). L1: the live flag is computed
truth — live = start known AND start_utc <= now; never filtered out (rule 7);
pregame detect rejects it exactly as before."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arb.config import load
from arb.venues.base import is_live

FIX = Path(__file__).resolve().parent / "fixtures"
CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")


def _fx(name):
    return json.loads((FIX / name).read_text())


def test_is_live_rule():
    now = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    assert is_live(now - timedelta(hours=1), now) is True
    assert is_live(now + timedelta(hours=1), now) is False
    assert is_live(None, now) is False  # start-unknown is never live


def test_kalshi_started_event_quote_arrives_live():
    from arb.venues.kalshi import parse_markets
    # fixture recorded 2026-07-08: the KC@NYM 19:10 ET start is permanently past
    quotes = parse_markets(CFG, _fx("kalshi_markets_total.json")["markets"],
                           sport="baseball_mlb", market_type="TOTAL")
    started = [q for q in quotes
               if q.venue_market_id.startswith("KXMLBTOTAL-26JUL081910KCNYM")]
    assert started and all(q.live for q in started)  # computed, not hardcoded
    # a future start is not live: rewrite the ticker year to 2099
    row = dict(_fx("kalshi_markets_total.json")["markets"][0])
    row["ticker"] = row["ticker"].replace("-26JUL", "-99JUL")
    future = parse_markets(CFG, [row], sport="baseball_mlb", market_type="TOTAL")
    assert future and all(not q.live for q in future)


def test_polymarket_started_event_quote_arrives_live():
    import copy

    from arb.venues.polymarket import quotes_from_event
    events = _fx("pm_gamma_mlb_games.json")
    book = _fx("pm_clob_book.json")
    ev = copy.deepcopy([e for e in events if any(m.get("sportsMarketType") == "moneyline"
                                                 for m in e["markets"])][0])
    # pin explicit past/future starts — never depend on the fixture date vs today
    for m in ev["markets"]:
        m["gameStartTime"] = "2020-01-01 00:00:00+00"
    quotes = quotes_from_event(CFG, ev, {book["asset_id"]: book}, sport="baseball_mlb")
    assert quotes and all(q.live for q in quotes)
    for m in ev["markets"]:
        m["gameStartTime"] = "2099-01-01 00:00:00+00"
    quotes = quotes_from_event(CFG, ev, {book["asset_id"]: book}, sport="baseball_mlb")
    assert quotes and all(not q.live for q in quotes)


def test_pregame_detect_still_rejects_live_groups():
    from arb.arbitrage import detect
    from arb.matching import build_groups, load_aliases
    from arb.venues.kalshi import parse_markets
    quotes = parse_markets(CFG, _fx("kalshi_markets_total.json")["markets"],
                           sport="baseball_mlb", market_type="TOTAL")
    started = [q for q in quotes
               if q.venue_market_id.startswith("KXMLBTOTAL-26JUL081910KCNYM")]
    groups = build_groups(started, CFG, load_aliases())
    opps, rejects = detect(CFG, groups)
    assert not opps
    assert any(r["reason"] == "in-play" for r in rejects)


def test_live_lane_config_keys_loaded():
    ll = CFG["live_lane"]
    assert ll["cadence_secs"] == 20
    from decimal import Decimal
    assert ll["kill"]["confirm_rate_floor"] == Decimal("0.10")
    assert "baseball_mlb" in ll["max_game_hours"]
    assert CFG["leadlag"]["min_matched_moves"] == 100


def test_shortlist_day_window_is_time_precise(tmp_path):
    # 'T'-separated ISO timestamps vs SQLite datetime('now') compare by DATE
    # only ('T' > ' '), so the "1 day" quote window stretched up to 2 days
    from datetime import datetime, timedelta, timezone

    from arb.lanes.live import shortlist
    from arb.store import Store
    store = Store(tmp_path / "sl.sqlite")
    now = datetime.now(timezone.utc)
    common = {"poll_id": 1, "event_key": "e", "category": "sports",
              "sport": "baseball_mlb", "venue": "kalshi", "book": "kalshi",
              "market_type": "WINNER", "line": None, "outcome": "mets",
              "side": "YES", "raw_price": "0.5", "cost": "0.5", "fee_cost": "0",
              "live": 1, "venue_market_id": "T1",
              "start_utc": (now - timedelta(hours=1)).isoformat()}
    store.insert("quotes", {**common,
                            "fetched_at": (now - timedelta(hours=25)).isoformat()})
    assert shortlist(CFG, store, now) == []  # stale quote: outside the window
    store.insert("quotes", {**common,
                            "fetched_at": (now - timedelta(hours=23)).isoformat()})
    assert [e["event_key"] for e in shortlist(CFG, store, now)] == ["e"]
