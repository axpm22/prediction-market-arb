import json
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.experimental.btc_sweep import parse_kalshi_btc, parse_polymarket_btc, run_btc_pass
from arb.experimental.whales import run_whale_pass
from arb.store import Store

WCFG = {"min_trades": 1, "wr_floor": 0.0, "wr_ceiling": 0.72, "min_category_share": 0.0}


# ---- Phase 4 BTC sweep ----

def test_parses_real_btc_fixtures() -> None:
    k = parse_kalshi_btc(json.loads(Path("tests/fixtures/kalshi_btc.json").read_text()))
    p = parse_polymarket_btc(json.loads(Path("tests/fixtures/polymarket_btc.json").read_text()))
    assert k and all(leg.oracle == "cfbenchmarks_brti" for leg in k)
    assert p and all(leg.oracle == "polymarket_uma" for leg in p)
    assert {leg.direction for leg in k} == {"UP", "DOWN"}


def test_btc_records_review_when_strike_and_window_align(tmp_path) -> None:
    config = load_config()
    store = Store(tmp_path / "a.sqlite")
    # Aligned strike+window across venues, still below 1 after practical fees -> REVIEW.
    kalshi = {"markets": [{"ticker": "KXBTCD-26JUN2107-T72000", "yes_sub_title": "above $72000",
              "close_time": "2026-06-21T11:00:00Z", "yes_ask_dollars": "0.40", "no_ask_dollars": "0.49"}]}
    poly = {"markets": [{"question": "Bitcoin above $72000 on June 21?",
            "endDate": "2026-06-21T11:00:00Z", "bestAsk": "0.50", "bestBid": "0.60"}]}
    n_legs, found = run_btc_pass(store, config, kalshi_fetch=lambda: kalshi, poly_fetch=lambda: poly)
    assert n_legs == 4 and found >= 1
    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets WHERE kind='ARB_BTC'"))
    assert rows
    # Cross-venue oracles differ -> REVIEW, trusted=0. (B1 single-venue YES+NO locks are OK.)
    cross = [r for r in rows if len({json.loads(r["legs_json"])[0]["venue"], json.loads(r["legs_json"])[1]["venue"]}) == 2]
    assert cross and all(r["status"] == "REVIEW" and int(r["trusted"]) == 0 for r in cross)


def test_btc_no_pair_when_strikes_differ(tmp_path) -> None:
    config = load_config()
    store = Store(tmp_path / "a.sqlite")
    kalshi = {"markets": [{"ticker": "KXBTCD-26JUN2107-T72000", "close_time": "2026-06-21T11:00:00Z",
              "yes_ask_dollars": "0.48", "no_ask_dollars": "0.49"}]}
    poly = {"markets": [{"question": "Bitcoin above $80000 on June 21?",
            "endDate": "2026-06-21T11:00:00Z", "bestAsk": "0.10", "bestBid": "0.08"}]}
    _, found = run_btc_pass(store, config, kalshi_fetch=lambda: kalshi, poly_fetch=lambda: poly)
    assert found == 0   # different strikes never pair


# ---- Phase 5 whale sweep ----

def test_whale_pass_ingests_real_trades_fixture(tmp_path) -> None:
    store = Store(tmp_path / "a.sqlite")
    trades = json.loads(Path("tests/fixtures/polymarket_trades.json").read_text())
    n_wallets, qualified = run_whale_pass(store, WCFG, trades=trades)
    assert n_wallets >= 1
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == n_wallets
        assert con.execute("SELECT COUNT(*) FROM wallet_trades").fetchone()[0] == len(trades)
