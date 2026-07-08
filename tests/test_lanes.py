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
