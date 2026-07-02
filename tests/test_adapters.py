from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.venues.kalshi import KalshiAdapter
from arb.venues.oddsapi import OddsApiAdapter
from arb.venues.polymarket import PolymarketAdapter
from arb.venues.sx import SxAdapter

FIXTURES = Path("tests/fixtures")


def test_each_adapter_parses_fixture_quotes() -> None:
    config = load_config()
    adapters = [
        OddsApiAdapter(config, FIXTURES),
        KalshiAdapter(config, FIXTURES),
        PolymarketAdapter(config, FIXTURES),
        SxAdapter(config, FIXTURES),
    ]
    for adapter in adapters:
        quotes = adapter.fetch_quotes("baseball_mlb")
        assert quotes
        assert all(Decimal("0") < quote.cost < Decimal("1") for quote in quotes)
        assert all(quote.fetched_at.tzinfo is not None for quote in quotes)
        assert all(quote.market_type in {"WINNER", "TOTAL"} for quote in quotes)


def test_totals_quotes_carry_exact_lines() -> None:
    config = load_config()
    for adapter in [OddsApiAdapter(config, FIXTURES), KalshiAdapter(config, FIXTURES),
                    PolymarketAdapter(config, FIXTURES), SxAdapter(config, FIXTURES)]:
        totals = [q for q in adapter.fetch_quotes("baseball_mlb") if q.market_type == "TOTAL"]
        assert all(q.line is not None for q in totals)
        assert all(q.outcome_label in {"OVER", "UNDER"} for q in totals)


def test_kalshi_recorded_fixture_prices_and_orderbook_depth() -> None:
    adapter = KalshiAdapter(load_config(), FIXTURES)
    quotes = adapter.fetch_quotes("baseball_mlb")
    winners = {q.outcome_label: q for q in quotes if q.market_type == "WINNER"}
    # K11: the quote is priced FROM the orderbook (1 - best opposing bid), not the /markets
    # snapshot — this very fixture has the two 9 points apart (snapshot 0.62 vs book 0.53),
    # a recorded instance of the intra-quote staleness the fix removes.
    assert winners["texas rangers"].cost == Decimal("0.5300")
    assert winners["kansas city royals"].cost == Decimal("0.4800")
    assert winners["texas rangers"].best_bid == Decimal("0.5200")
    # Live markets show liquidity_dollars 0 with deep books; depth must come from the orderbook.
    assert all(q.depth_usd is not None and q.depth_usd > 0 for q in winners.values())
