"""Mapped-pair lane: nonsports_map.yaml -> both venues' quotes under one
event_key, inversion normalized, locked cross-venue covers booked
XSPREAD/review. Fakes only (rule 2 ethos)."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.nonsports import mapped
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

ENTRY_INV = {"kalshi": "KXU3-26JUL-T3.9", "polymarket": "0xabc",
             "event_key": "nonsports:map:kxu3-26jul-t3.9",
             "inverted": True, "concept": "unemployment"}


def _fake_kalshi(monkeypatch, yes_ask="0.44", yes_bid="0.41",
                 no_ask="0.59", no_bid="0.56"):
    def fake_get(url, cfg, deadline=None):
        assert "/markets/KXU3-26JUL-T3.9" in url
        return {"market": {"status": "open",
                           "yes_ask_dollars": yes_ask, "yes_bid_dollars": yes_bid,
                           "no_ask_dollars": no_ask, "no_bid_dollars": no_bid,
                           "yes_ask_size_fp": "100", "yes_bid_size_fp": "100"}}
    monkeypatch.setattr(mapped, "get_json", fake_get)


def _fake_pm(monkeypatch, yes_ask="0.52", no_ask="0.50"):
    def fake_get(url, cfg, deadline=None):
        return [{"closed": False, "clobTokenIds": json.dumps(["tY", "tN"])}]

    def fake_post(url, cfg, payload, deadline=None):
        return [{"asset_id": "tY",
                 "asks": [{"price": yes_ask, "size": "80"}],
                 "bids": [{"price": "0.49", "size": "80"}]},
                {"asset_id": "tN",
                 "asks": [{"price": no_ask, "size": "80"}],
                 "bids": [{"price": "0.47", "size": "80"}]}]
    return fake_get, fake_post


def test_inverted_pair_normalizes_sides_and_keeps_venue_side(monkeypatch):
    fake_get, fake_post = _fake_pm(monkeypatch)
    monkeypatch.setattr(mapped, "get_json", fake_get)
    monkeypatch.setattr(mapped, "post_json", fake_post)
    qs = mapped.pm_pair_quotes(CFG, ENTRY_INV, NOW)
    assert {(q.side, q.venue_side) for q in qs} == {("NO", "YES"), ("YES", "NO")}
    assert all(q.event_key == ENTRY_INV["event_key"] for q in qs)


def test_locked_cross_venue_cover_books_xspread_review(monkeypatch, tmp_path):
    # K YES 0.44 (+quadratic fee) with PM(normalized) NO 0.52: sum < 1 -> locked
    store = Store(tmp_path / "m.sqlite")

    def router_get(url, cfg, deadline=None):
        if "/markets/KXU3" in url:
            return {"market": {"status": "open",
                               "yes_ask_dollars": "0.44", "yes_bid_dollars": "0.41",
                               "no_ask_dollars": "0.59", "no_bid_dollars": "0.56",
                               "yes_ask_size_fp": "100", "yes_bid_size_fp": "100"}}
        return [{"closed": False, "clobTokenIds": json.dumps(["tY", "tN"])}]

    _, fake_post = _fake_pm(monkeypatch)
    monkeypatch.setattr(mapped, "get_json", router_get)
    monkeypatch.setattr(mapped, "post_json", fake_post)
    monkeypatch.setattr(mapped, "load_map", lambda path=None: [ENTRY_INV])
    s = mapped.run_mapped(CFG, store, now=NOW)
    assert s["pairs"] == 1 and s["quotes"] == 4 and s["errors"] == 0
    # quotes persisted under the shared key (rule 7)
    n = store.query("SELECT COUNT(*) c FROM quotes WHERE event_key=?",
                    (ENTRY_INV["event_key"],))[0]["c"]
    assert n == 4
    bets = [dict(r) for r in store.query("SELECT * FROM flat_bets")]
    assert bets, "locked cover must book"
    assert all(b["kind"] == "XSPREAD" and b["lane"] == "review" for b in bets)
    assert all(b["venue_pair"] == "kalshi:polymarket" for b in bets)


def test_no_lock_no_bet_but_quotes_still_persist(monkeypatch, tmp_path):
    store = Store(tmp_path / "n.sqlite")

    def router_get(url, cfg, deadline=None):
        if "/markets/KXU3" in url:
            return {"market": {"status": "open",
                               "yes_ask_dollars": "0.55", "yes_bid_dollars": "0.52",
                               "no_ask_dollars": "0.48", "no_bid_dollars": "0.45",
                               "yes_ask_size_fp": "100", "yes_bid_size_fp": "100"}}
        return [{"closed": False, "clobTokenIds": json.dumps(["tY", "tN"])}]

    _, fake_post = _fake_pm(monkeypatch, yes_ask="0.56", no_ask="0.54")
    monkeypatch.setattr(mapped, "get_json", router_get)
    monkeypatch.setattr(mapped, "post_json", fake_post)
    monkeypatch.setattr(mapped, "load_map", lambda path=None: [ENTRY_INV])
    s = mapped.run_mapped(CFG, store, now=NOW)
    assert s["quotes"] == 4
    assert not store.query("SELECT 1 FROM flat_bets")
