from pathlib import Path

from arb.config import load_config
from arb.matching import normalize_name
from arb.venues.kalshi import KalshiAdapter
from arb.venues.oddsapi import OddsApiAdapter
from arb.venues.polymarket import PolymarketAdapter
from arb.venues.sx import SxAdapter

COMMON_KEY = "baseball_mlb:2026-06-10:kansas city royals:texas rangers"


def test_aliases_are_sport_specific() -> None:
    assert normalize_name("Rangers", "baseball_mlb") == "texas rangers"


def test_kalshi_short_city_names_resolve() -> None:
    assert normalize_name("Texas", "baseball_mlb") == "texas rangers"
    assert normalize_name("Kansas City", "baseball_mlb") == "kansas city royals"
    assert normalize_name("Chicago WS", "baseball_mlb") == "chicago white sox"
    assert normalize_name("Los Angeles A", "baseball_mlb") == "los angeles angels"


def test_same_game_on_four_venues_maps_to_one_event_key() -> None:
    config = load_config()
    fixture_dir = Path("tests/fixtures")
    for adapter in [
        OddsApiAdapter(config, fixture_dir),
        KalshiAdapter(config, fixture_dir),
        PolymarketAdapter(config, fixture_dir),
        SxAdapter(config, fixture_dir),
    ]:
        keys = {quote.event_key for quote in adapter.fetch_quotes("baseball_mlb")}
        assert COMMON_KEY in keys, f"{adapter.name} missing {COMMON_KEY}"
