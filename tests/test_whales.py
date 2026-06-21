from decimal import Decimal

from arb.experimental.whales import copy_fill_slippage, is_uncopyable_category, wallet_stats

CFG = {"min_trades": 100, "wr_floor": 0.60, "wr_ceiling": 0.72, "min_category_share": 0.50}


def trades_for(addr: str, n: int, condition_prefix: str) -> list[dict]:
    return [{"proxyWallet": addr, "conditionId": f"{condition_prefix}{i}", "side": "BUY",
             "price": 0.5, "size": 1} for i in range(n)]


def test_qualifying_wallet() -> None:
    addr = "0xwhale"
    ts = trades_for(addr, 150, "c")
    settled = {f"c{i}": (i < 99) for i in range(150)}   # 99/150 settled... use settled subset
    # Make exactly settled markets: 100 settled, 66 wins -> 0.66 WR (in band).
    settled = {f"c{i}": (i < 66) for i in range(100)}
    cats = {f"c{i}": "politics" for i in range(150)}    # single-category concentration 1.0
    [w] = [w for w in wallet_stats(ts, settled, cats, CFG) if w.address == addr]
    assert w.n_trades == 150
    assert abs(w.win_rate - 0.66) < 0.01
    assert w.top_category_share == 1.0
    assert w.qualified is True
    assert w.suspect is False


def test_too_few_trades_not_qualified() -> None:
    ts = trades_for("0xsmall", 80, "c")
    settled = {f"c{i}": (i < 53) for i in range(80)}
    cats = {f"c{i}": "politics" for i in range(80)}
    [w] = wallet_stats(ts, settled, cats, CFG)
    assert w.qualified is False


def test_high_win_rate_is_suspect() -> None:
    ts = trades_for("0xzombie", 150, "c")
    settled = {f"c{i}": (i < 130) for i in range(150)}  # ~0.87 WR -> above ceiling
    cats = {f"c{i}": "politics" for i in range(150)}
    [w] = wallet_stats(ts, settled, cats, CFG)
    assert w.suspect is True
    assert w.qualified is False   # above ceiling fails the band


def test_copy_fill_slippage_is_adverse_selection_cost() -> None:
    # whale bought at 0.40, you fill after the move at 0.44 -> +0.04 against you.
    assert copy_fill_slippage(Decimal("0.40"), Decimal("0.44")) == Decimal("0.040000")


def test_uncopyable_bot_categories_skipped() -> None:
    assert is_uncopyable_category("btc_15m", ["btc_15m", "eth_15m"]) is True
    assert is_uncopyable_category("politics", ["btc_15m", "eth_15m"]) is False
