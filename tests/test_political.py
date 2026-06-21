from decimal import Decimal

from arb.config import load_config
from arb.pricing import q6
from arb.store import Store
from arb.experimental.political.intramarket import find_intramarket_arb
from arb.experimental.political.sweep import run_political_pass

FEE = Decimal("0.07")


def market(ticker: str, yes: str, no: str, liq: str = "500") -> dict:
    return {
        "ticker": ticker, "title": f"{ticker} question?",
        "yes_ask_dollars": yes, "no_ask_dollars": no,
        "yes_bid_dollars": "0.10", "liquidity_dollars": liq,
        "close_time": "2026-06-20T00:00:00Z",
    }


def test_locked_when_yes_plus_no_below_one() -> None:
    # 0.45 + 0.45 = 0.90; fees 0.07*0.45*0.55=0.017325 each → total 0.93465 < 1.
    arb = find_intramarket_arb(market("KXFED", "0.45", "0.45"), FEE, "Politics")
    assert arb is not None
    assert arb.total_cost == Decimal("0.934650")
    assert arb.roi == q6(Decimal("1") / Decimal("0.934650") - Decimal("1"))


def test_no_arb_when_sides_sum_to_one_or_more() -> None:
    assert find_intramarket_arb(market("KXFED", "0.55", "0.50"), FEE, "Politics") is None


def test_empty_book_is_not_an_arb() -> None:
    empty = {"ticker": "KXX", "yes_ask_dollars": "1.0", "no_ask_dollars": "0.01",
             "yes_bid_dollars": "0.0", "liquidity_dollars": "0", "close_time": "2026-06-20T00:00:00Z"}
    assert find_intramarket_arb(empty, FEE, "Politics") is None


def test_sweep_persists_arbs_trusted_and_dedupes(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "arb.sqlite")
    config = load_config()
    pcfg = {"watch_interval_secs": 180, "categories": ["Politics"]}

    import arb.experimental.political.sweep as sweep
    monkeypatch.setattr(sweep, "load_series", lambda cats: [{"ticker": "KXFED", "category": "Politics"}])
    fetcher = lambda ticker: [market("KXFED-A", "0.45", "0.45"), market("KXFED-B", "0.55", "0.50")]

    scanned, found = run_political_pass(store, config, pcfg, fetcher=fetcher)
    assert (scanned, found) == (2, 1)
    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets WHERE category='politics'"))
    assert len(rows) == 1
    assert rows[0]["trusted"] == 1
    assert rows[0]["kind"] == "ARB"

    # Re-run: the still-open arb is not re-bet (dedup by fingerprint).
    run_political_pass(store, config, pcfg, fetcher=fetcher)
    with store.connect() as con:
        assert len(list(con.execute("SELECT 1 FROM flat_bets WHERE category='politics'"))) == 1
