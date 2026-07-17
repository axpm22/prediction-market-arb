"""Phase 8 lanes: political intra-market math, settler registry startup check."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import ConfigError, load
from arb.lanes.political import scan_market
from arb.settlement import validate_settlement_paths

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")


def _market(yes_ask="0.47", no_ask="0.49"):
    return ({"conditionId": "0xabc", "question": "Will X happen?",
             "clobTokenIds": json.dumps(["t-yes", "t-no"]), "volume24hr": 42},
            {"t-yes": {"asks": [{"price": yes_ask, "size": "100"}],
                       "bids": [{"price": "0.45", "size": "50"}]},
             "t-no": {"asks": [{"price": no_ask, "size": "100"}],
                      "bids": [{"price": "0.47", "size": "50"}]}})


def test_political_yes_no_under_one_flags():
    market, books = _market()
    o = scan_market(CFG, market, books)
    assert o is not None and o.kind == "POLITICAL"
    # 0.47 + 0.49 + politics fees (0.04·p·(1-p) each) = 0.97996 -> edge 0.02004
    assert o.edge == Decimal("0.02004")
    assert o.status == "OK"
    assert {leg.side for leg in o.legs} == {"YES", "NO"}


def test_political_above_one_never_flags():
    market, books = _market(yes_ask="0.52", no_ask="0.50")
    assert scan_market(CFG, market, books) is None


def test_political_one_sided_book_never_flags():
    market, books = _market()
    books["t-no"]["asks"] = []
    assert scan_market(CFG, market, books) is None


def test_political_stale_warn_books_review():
    market, books = _market(yes_ask="0.45", no_ask="0.49")  # roi ~6.3% > 4%
    o = scan_market(CFG, market, books)
    assert o is not None and o.status == "REVIEW" and "stale-warn" in o.notes


def test_btc_without_settler_is_startup_error():
    cfg = load(Path(__file__).resolve().parent.parent / "config.yaml")
    cfg["btc_arb"]["enabled"] = True
    with pytest.raises(ConfigError, match="settlement path"):
        validate_settlement_paths(cfg)
    cfg["btc_arb"]["enabled"] = False
    validate_settlement_paths(cfg)  # default config passes


def test_political_golden_rows_and_opportunity_all_fields():
    # Golden: pins BOTH functions' full output (multi-level book so best-ask/
    # best-bid selection is exercised) across the parse-once refactor.
    from datetime import datetime, timezone

    from arb.lanes.political import book_quote_rows
    market = {"conditionId": "0xabc", "question": "Will X happen?",
              "clobTokenIds": json.dumps(["t-yes", "t-no"]), "volume24hr": 42}
    books = {"t-yes": {"asks": [{"price": "0.47", "size": "100"},
                                {"price": "0.48", "size": "10"}],
                       "bids": [{"price": "0.45", "size": "50"},
                                {"price": "0.44", "size": "5"}]},
             "t-no": {"asks": [{"price": "0.49", "size": "100"}],
                      "bids": [{"price": "0.47", "size": "50"}]}}
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    rows = book_quote_rows(CFG, market, books, now)
    assert rows == [
        {"fetched_at": "2026-07-17T12:00:00+00:00",
         "event_key": "nonsports:pm:0xabc", "category": "nonsports",
         "sport": "politics", "venue": "polymarket", "book": "polymarket",
         "market_type": "WINNER", "line": None, "outcome": "Will X happen?",
         "side": "YES", "raw_price": Decimal("0.47"), "cost": Decimal("0.47"),
         "fee_cost": Decimal("0.009964"), "depth_usd": Decimal("47.00"),
         "ladder_json": None, "live": False, "bid": Decimal("0.45"),
         "volume_24h": Decimal("42"), "start_utc": None,
         "start_grade": "absent", "venue_market_id": "0xabc:t-yes"},
        {"fetched_at": "2026-07-17T12:00:00+00:00",
         "event_key": "nonsports:pm:0xabc", "category": "nonsports",
         "sport": "politics", "venue": "polymarket", "book": "polymarket",
         "market_type": "WINNER", "line": None, "outcome": "Will X happen?",
         "side": "NO", "raw_price": Decimal("0.49"), "cost": Decimal("0.49"),
         "fee_cost": Decimal("0.009996"), "depth_usd": Decimal("49.00"),
         "ladder_json": None, "live": False, "bid": Decimal("0.47"),
         "volume_24h": Decimal("42"), "start_utc": None,
         "start_grade": "absent", "venue_market_id": "0xabc:t-no"}]
    o = scan_market(CFG, market, books)
    assert (o.kind, o.edge, o.status, o.notes) == \
        ("POLITICAL", Decimal("0.020040"), "OK", "yes+no=0.979960")
    assert [(leg.side, leg.venue_side, leg.cost, leg.bid, leg.fee_cost,
             leg.depth_usd, leg.venue_market_id, leg.outcome_label,
             leg.volume_24h, leg.event_key) for leg in o.legs] == \
        [("YES", "YES", Decimal("0.47"), Decimal("0.45"), Decimal("0.009964"),
          Decimal("47.00"), "0xabc:t-yes", "Will X happen?", Decimal("42"),
          "nonsports:pm:0xabc"),
         ("NO", "NO", Decimal("0.49"), Decimal("0.47"), Decimal("0.009996"),
          Decimal("49.00"), "0xabc:t-no", "Will X happen?", Decimal("42"),
          "nonsports:pm:0xabc")]
