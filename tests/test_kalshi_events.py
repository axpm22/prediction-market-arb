"""negrisk.kalshi_events (implemented 2026-07-14): Kalshi ME event groups ->
scan_group baskets, discovery via the verified global open-markets listing.
Fakes only (rule 2)."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.nonsports import kalshi_events as ke

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _mkt(ticker, event, yes_ask, yes_bid, no_ask, no_bid, sub):
    return {"ticker": ticker, "event_ticker": event, "title": "Who wins?",
            "yes_sub_title": sub, "yes_ask_dollars": yes_ask,
            "yes_bid_dollars": yes_bid, "no_ask_dollars": no_ask,
            "no_bid_dollars": no_bid, "yes_ask_size_fp": "50",
            "volume_24h_fp": "10",
            "expected_expiration_time": "2026-07-16T15:00:00Z"}


def _fakes(monkeypatch, mutually_exclusive=True):
    # NO-basket that survives Kalshi's quadratic fee (unlike the fee-free
    # geopolitics vector): NO asks 0.64x3 = 1.92 + ~0.048 fees = 1.968 < 2
    group = [_mkt(f"KXWHO-26AUG-C{i}", "KXWHO-26AUG", "0.3405", "0.3375",
                  "0.64", "0.6375", f"o{i}") for i in range(3)]
    events = [{"event_ticker": "KXWHO-26AUG", "category": "Politics",
               "title": "Who wins?", "mutually_exclusive": mutually_exclusive,
               "markets": group},
              {"event_ticker": "KXMLB-X", "category": "Sports", "title": "x",
               "mutually_exclusive": True,
               "markets": [_mkt(f"KXMLB-X-{i}", "KXMLB-X", "0.5", "0.48",
                                "0.52", "0.5", "x") for i in range(3)]}]

    def fake_get(url, cfg, deadline=None):
        assert "/events?" in url and "with_nested_markets=true" in url
        return {"events": events, "cursor": ""}

    monkeypatch.setattr(ke, "get_json", fake_get)


def test_me_group_baskets_and_persists_quotes(monkeypatch):
    _fakes(monkeypatch)
    quotes, baskets, checked = ke.scan_kalshi_events(CFG, NOW)
    assert checked == 1
    # the sports-series stray never enters a group
    assert all(q["event_key"] == "nonsports:kalshi:KXWHO-26AUG" for q in quotes)
    assert len(quotes) == 6  # 3 outcomes x YES+NO
    no = [b for b in baskets if b["side"] == "NO"]
    assert no and no[0]["proof_status"] == "proven"
    assert no[0]["venue_pair"] == "kalshi:self"
    assert no[0]["resolution_date"] == "2026-07-16T15:00:00Z"
    assert Decimal(no[0]["edge_points"]) > 0


def test_without_venue_flag_basket_stays_unproven(monkeypatch):
    _fakes(monkeypatch, mutually_exclusive=False)
    _, baskets, _ = ke.scan_kalshi_events(CFG, NOW)
    no = [b for b in baskets if b["side"] == "NO"]
    assert no and no[0]["proof_status"] == "unproven-exhaustive"
    assert no[0]["status"] == "REVIEW"
