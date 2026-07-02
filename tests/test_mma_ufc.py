"""FIX_POLYMARKET_UFC_TAG: Polymarket MMA discovery moved from the rotted `mma` tag (futures
only) to `ufc` (fight cards), plus the Kalshi MMA adapter fix that lets the same fight join
across venues. Fixtures are recorded live 2026-07-02 (public, unauthenticated; no Odds API)."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.activity import active_sports
from arb.config import load_config
from arb.venues.kalshi import KalshiAdapter
from arb.venues.polymarket import PolymarketAdapter

FIXTURES = Path("tests/fixtures")


def test_polymarket_ufc_fixture_winner_and_totals() -> None:
    quotes = PolymarketAdapter(load_config(), FIXTURES).fetch_quotes("mma")
    winners = [q for q in quotes if q.market_type == "WINNER"]
    totals = [q for q in quotes if q.market_type == "TOTAL"]
    # In-scope market types both flow (moneyline -> WINNER, round totals -> TOTAL)...
    assert winners, "expected UFC moneyline (winner) quotes from the ufc-tag fixture"
    assert totals, "expected UFC round-total quotes from the ufc-tag fixture"
    # ...and every quote is a scoped type — method-of-victory / go-the-distance never leak.
    assert all(q.market_type in {"WINNER", "TOTAL"} for q in quotes)
    # Round totals round-trip as Decimal lines with OVER/UNDER labels.
    assert all(isinstance(q.line, Decimal) and q.outcome_label in {"OVER", "UNDER"} for q in totals)
    assert all(Decimal("0") < q.cost < Decimal("1") for q in quotes)


def test_ufc_fixture_ignores_method_and_distance_markets() -> None:
    # The fixture deliberately carries ufc_method_of_victory / ufc_go_the_distance markets;
    # they must produce zero quotes (out of v1 scope, SPEC §1).
    import json
    types = {m.get("sportsMarketType")
             for e in json.loads((FIXTURES / "polymarket_mma.json").read_text())
             for m in e["markets"]}
    assert {"ufc_method_of_victory", "ufc_go_the_distance"} <= types, "fixture must include the out-of-scope types"
    quotes = PolymarketAdapter(load_config(), FIXTURES).fetch_quotes("mma")
    assert quotes and all(q.market_type in {"WINNER", "TOTAL"} for q in quotes)


def test_old_mma_tag_futures_fixture_yields_zero() -> None:
    # Regression: the rotted `tag_slug=mma` response (futures-style, no gameStartTime / recognized
    # sportsMarketType) parses to zero quotes without raising — documents why the tag changed.
    quotes = PolymarketAdapter(load_config(), FIXTURES).fetch_quotes("mma_futures_only")
    assert quotes == []


def test_kalshi_polymarket_mma_same_fight_joins() -> None:
    config = load_config()
    poly = PolymarketAdapter(config, FIXTURES).fetch_quotes("mma")
    kalshi = KalshiAdapter(config, FIXTURES).fetch_quotes("mma")
    assert kalshi, "Kalshi MMA fixture should yield fighter quotes"
    # Kalshi MMA legs must be timed (real fight date from the ticker), never dropped as
    # start-unknown — otherwise detection discards them before any cross-venue pairing.
    assert all("start-unknown" not in q.notes and q.start_utc is not None for q in kalshi)
    poly_keys = {q.event_key for q in poly if q.market_type == "WINNER"}
    kalshi_keys = {q.event_key for q in kalshi}
    shared = poly_keys & kalshi_keys
    assert shared, f"same fight should share an event_key; poly={poly_keys} kalshi={kalshi_keys}"
    # Both fighter labels align across venues within the shared fight (the arb needs both legs).
    key = next(iter(shared))
    poly_labels = {q.outcome_label for q in poly if q.event_key == key and q.market_type == "WINNER"}
    kalshi_labels = {q.outcome_label for q in kalshi if q.event_key == key}
    assert poly_labels == kalshi_labels, f"fighter labels differ: poly={poly_labels} kalshi={kalshi_labels}"
    # Both venues anchor an MMA leg to the same ET-hour of the shared fight date -> identical
    # start (0 drift), so detection's start clustering / start-drift gate can't split the pair.
    assert {q.start_utc for q in poly if q.event_key == key} == {q.start_utc for q in kalshi}


def test_mma_active_on_fight_day() -> None:
    # Activity gate: MMA reads ACTIVE across the fight's ET day (a midnight anchor would already be
    # in the past by fight time and wrongly mark the sport dormant on the day it matters).
    config = load_config()
    quotes = (PolymarketAdapter(config, FIXTURES).fetch_quotes("mma")
              + KalshiAdapter(config, FIXTURES).fetch_quotes("mma"))
    fight_day_afternoon = datetime(2026, 7, 11, 18, tzinfo=timezone.utc)
    assert "mma" in active_sports(quotes, fight_day_afternoon, config.activity_lookahead_hours)
