"""World Cup consensus — Odds-API sport-key normalization so consensus joins venue quotes.

The bug it locks: the Odds-API key `soccer_fifa_world_cup` is used verbatim as the event_key
prefix, while the venues key the same match as `soccer`. The two namespaces never join, so a
World Cup consensus would be fetched (spending credits) and match ZERO venue quotes. canonical_sport
normalizes the Odds-API side to the venue-canonical prefix.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.cli import compute_consensus, detect_ev_from_consensus
from arb.config import load_config
from arb.matching import canonical_sport, event_key
from arb.models import Quote
from arb.venues.oddsapi import parse_odds_events

FIX = Path("tests/fixtures")
ALIASES = {"soccer_fifa_world_cup": "soccer"}
START = datetime(2026, 7, 9, 18, 0, tzinfo=timezone.utc)


# ---- 1. canonical_sport ----

def test_canonical_sport_maps_world_cup_and_is_identity_otherwise() -> None:
    assert canonical_sport("soccer_fifa_world_cup", ALIASES) == "soccer"
    assert canonical_sport("baseball_mlb", ALIASES) == "baseball_mlb"   # already its venue prefix
    assert canonical_sport("tennis_atp_wimbledon", ALIASES) == "tennis_atp_wimbledon"  # unmapped -> identity
    assert canonical_sport("soccer_fifa_world_cup", None) == "soccer_fifa_world_cup"   # no aliases -> identity


# ---- 2. the join regression (the heart of the fix) ----

def test_oddsapi_world_cup_event_key_joins_venue_key() -> None:
    events = json.loads((FIX / "oddsapi_soccer_fifa_world_cup.json").read_text())
    quotes = parse_odds_events(events, "soccer_fifa_world_cup", ALIASES)
    assert quotes, "fixture should produce consensus quotes"
    oddsapi_key = quotes[0].event_key
    # what a venue (kalshi/poly/sx) builds for the same match, keyed on the canonical `soccer`:
    venue_key = event_key("soccer", "Germany", "Ecuador", START)
    assert oddsapi_key == venue_key                       # byte-equal -> consensus will match
    assert oddsapi_key == "soccer:2026-07-09:ecuador:germany"
    # Without normalization the prefix would be the raw Odds-API key and would NOT join:
    raw_key = parse_odds_events(events, "soccer_fifa_world_cup", None)[0].event_key
    assert raw_key.startswith("soccer_fifa_world_cup:") and raw_key != venue_key


# ---- 3. end-to-end: a venue soccer quote priced under the World Cup consensus yields EV ----

def _venue_quote(outcome: str, cost: str) -> Quote:
    c = Decimal(cost)
    return Quote(event_key="soccer:2026-07-09:ecuador:germany", venue="kalshi", book="KXWC",
                 market_type="WINNER", line=None, outcome_label=outcome, raw_price=c, cost=c,
                 fee_cost=Decimal("0"), depth_usd=Decimal("500"), live=False,
                 fetched_at=datetime.now(timezone.utc), start_utc=START,
                 home="germany", away="ecuador")


def test_world_cup_consensus_prices_a_venue_quote_into_ev() -> None:
    events = json.loads((FIX / "oddsapi_soccer_fifa_world_cup.json").read_text())
    consensus_quotes = parse_odds_events(events, "soccer_fifa_world_cup", ALIASES)
    config = load_config()
    raw = compute_consensus(consensus_quotes, config.min_books_for_consensus,
                            config.min_books_for_consensus_totals, method=config.devig_method)
    assert ("soccer:2026-07-09:ecuador:germany", "germany") in raw   # consensus row exists & joins
    now = datetime.now(timezone.utc)
    consensus = {k: (v, now) for k, v in raw.items()}
    # Germany is the heavy favorite (~0.60 fair); a kalshi quote at 0.50 is +EV.
    opps = detect_ev_from_consensus([_venue_quote("germany", "0.50")], consensus,
                                    max_age_hours=8, trusted_pairs=set(), config=config)
    assert opps, "a venue soccer quote under the World Cup consensus must produce an EV opportunity"
    assert opps[0].legs[0].outcome_label == "germany"

    # The bug-state proof: with the raw (un-normalized) prefix, consensus is keyed
    # soccer_fifa_world_cup:... and the soccer: venue quote finds NO match -> zero EV.
    raw_unjoined = compute_consensus(parse_odds_events(events, "soccer_fifa_world_cup", None),
                                     config.min_books_for_consensus, config.min_books_for_consensus_totals,
                                     method=config.devig_method)
    unjoined = {k: (v, now) for k, v in raw_unjoined.items()}
    assert detect_ev_from_consensus([_venue_quote("germany", "0.50")], unjoined,
                                    max_age_hours=8, trusted_pairs=set(), config=config) == []
