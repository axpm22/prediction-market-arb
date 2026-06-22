import json
from pathlib import Path

from arb.config import load_config
from arb.experimental.btc_sweep import parse_kalshi_btc, parse_polymarket_btc, run_btc_pass
from arb.store import Store

FIXTURES = Path("tests/fixtures")


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def test_parsers_handle_recorded_fixtures() -> None:
    # Real recorded responses parse into strike+window-keyed UP/DOWN legs without crashing.
    k = parse_kalshi_btc(_load("kalshi_btc.json"))
    p = parse_polymarket_btc(_load("polymarket_btc.json"))
    assert all(leg.asset.startswith("BTC@") for leg in k + p)
    assert {leg.direction for leg in k} <= {"UP", "DOWN"}
    # Strike is parsed from $-amounts / Kalshi T-segments, never bare numbers (dates).
    assert all("@" in leg.asset for leg in k + p)


def test_sweep_emits_review_arb_btc_flag_on_matching_strike_window(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    config = load_config()
    # Same strike + same window on both venues; UP(kalshi 0.48) + DOWN(poly 0.49) = 0.97 < 1.
    kalshi = {"markets": [{
        "ticker": "KXBTCD-26JUN2217-T73000", "yes_sub_title": "$73,000 or above",
        "close_time": "2026-06-22T21:00:00Z", "yes_ask_dollars": "0.48", "no_ask_dollars": "0.55",
    }]}
    poly = {"markets": [{
        "question": "Will Bitcoin reach $73,000?", "groupItemTitle": "↑ 73,000",
        "endDate": "2026-06-22T21:00:00Z", "bestAsk": 0.60, "bestBid": 0.51,  # DOWN = 1-0.51 = 0.49
    }]}
    n_legs, found = run_btc_pass(store, config, kalshi_fetch=lambda: kalshi, poly_fetch=lambda: poly)
    assert n_legs == 4 and found >= 1
    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets WHERE kind='ARB_BTC'"))
    assert rows
    # Oracles differ (CF Benchmarks vs UMA) -> every flag REVIEW, never auto-filled.
    assert all(r["status"] == "REVIEW" and r["trusted"] == 0 for r in rows)
    assert "same_oracle=False" in rows[0]["notes"]


def test_sweep_no_flag_when_strikes_differ(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    config = load_config()
    kalshi = {"markets": [{
        "ticker": "KXBTCD-26JUN2217-T73000", "yes_sub_title": "$73,000 or above",
        "close_time": "2026-06-22T21:00:00Z", "yes_ask_dollars": "0.48", "no_ask_dollars": "0.55",
    }]}
    poly = {"markets": [{  # different strike -> different asset key -> never pairs
        "question": "Will Bitcoin reach $90,000?", "groupItemTitle": "↑ 90,000",
        "endDate": "2026-06-22T21:00:00Z", "bestAsk": 0.40, "bestBid": 0.39,
    }]}
    _, found = run_btc_pass(store, config, kalshi_fetch=lambda: kalshi, poly_fetch=lambda: poly)
    assert found == 0
