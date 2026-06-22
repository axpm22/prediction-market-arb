import json
from pathlib import Path

from arb.experimental.whales import run_whale_pass
from arb.store import Store

CFG = {"min_trades": 100, "wr_floor": 0.60, "wr_ceiling": 0.72, "min_category_share": 0.50}


def test_whale_pass_ingests_real_fixture_into_ledger(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    trades = json.loads(Path("tests/fixtures/polymarket_trades.json").read_text())
    n_wallets, qualified = run_whale_pass(store, CFG, trades=trades)
    assert n_wallets >= 1
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == n_wallets
        assert con.execute("SELECT COUNT(*) FROM wallet_trades").fetchone()[0] == len(trades)


def test_whale_pass_qualifies_a_heavy_single_lane_wallet(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    trades = [{"proxyWallet": "0xbig", "conditionId": f"c{i}", "side": "BUY",
               "price": 0.5, "size": 1} for i in range(150)]
    n_wallets, qualified = run_whale_pass(store, CFG, trades=trades)
    assert n_wallets == 1
    with store.connect() as con:
        row = con.execute("SELECT n_trades, category_share FROM wallets WHERE address='0xbig'").fetchone()
    assert row["n_trades"] == 150          # >= min_trades
    assert row["category_share"] == 1.0    # single lane (all 'unknown' until enrichment)
