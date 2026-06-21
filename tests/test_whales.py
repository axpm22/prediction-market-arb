from decimal import Decimal

from arb.experimental.whales import (
    copy_fill_slippage,
    is_uncopyable_category,
    persist_wallet_stats,
    record_copy_bet,
    record_wallet_trades,
    wallet_stats,
)
from arb.store import Store

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
    assert w.top_category == "politics"
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
    assert w.qualified is True    # kept for cohort testing, but flagged suspect


def test_copy_fill_slippage_is_adverse_selection_cost() -> None:
    # whale bought at 0.40, you fill after the move at 0.44 -> +0.04 against you.
    assert copy_fill_slippage(Decimal("0.40"), Decimal("0.44")) == Decimal("0.040000")


def test_uncopyable_bot_categories_skipped() -> None:
    assert is_uncopyable_category("btc_15m", ["btc_15m", "eth_15m"]) is True
    assert is_uncopyable_category("politics", ["btc_15m", "eth_15m"]) is False


def test_whale_stats_and_copy_bet_persist_to_ledger(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    ts = trades_for("0xwhale", 150, "c")
    settled = {f"c{i}": (i < 66) for i in range(100)}
    cats = {f"c{i}": "politics" for i in range(150)}
    stats = wallet_stats(ts, settled, cats, CFG)

    persist_wallet_stats(store, stats)
    assert record_wallet_trades(store, ts[:2]) == 2
    poll = store.start_poll("whales")
    assert record_copy_bet(
        store,
        poll,
        address="0xwhale",
        condition_id="condition-1",
        category="politics",
        side="YES",
        whale_price=Decimal("0.40"),
        next_executable_price=Decimal("0.44"),
        size_usd=Decimal("25"),
        market_title="Will this test pass?",
    ) == 1

    with store.connect() as con:
        [wallet] = list(con.execute("SELECT * FROM wallets"))
        assert int(wallet["qualified"]) == 1
        assert wallet["top_category"] == "politics"
        assert con.execute("SELECT COUNT(*) FROM wallet_trades").fetchone()[0] == 2
        [copy] = list(con.execute("SELECT * FROM flat_bets WHERE kind='COPY'"))

    assert copy["optimistic_cost"] == "0.40"
    assert copy["confirmed_cost"] == "0.44"
    assert copy["edge"] == "-0.040000"
    assert copy["trusted"] == 0
    assert "slippage=0.040000" in copy["notes"]
