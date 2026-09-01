"""Kalshi ask-ladders on the covers path.

Two things are pinned here, both of which have burned this project before:

  1. The INVERSION (rule 10's lesson, applied to Kalshi). `orderbook_fp` carries
     per-side BID levels; the ask of one side derives as 1 - the opposite side's
     best bid. Getting this backwards silently flips every Kalshi ladder, which
     is exactly how the SX adapter could have gone wrong.
  2. The GATE. Fetching a ladder costs one REST call PER MARKET, so it happens
     only for opportunities that already cleared edge_net>0 and spread<=edge.
     A test that lets it fetch for everything would hide a ~100x cost blowup.

No network: the fetcher is injected, and the orderbook fixture is the recorded
one already cited in data/verified_facts.md.
"""
import json
from decimal import Decimal

import pytest
from pathlib import Path

from arb import covers, cycle
from arb.config import load
from arb.venues import kalshi

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")
OB = json.loads((ROOT / "tests/fixtures/kalshi_orderbook.json").read_text())["orderbook_fp"]


def _q(venue, side, cost, bid, ticker="KXTEST", ladder=None):
    from datetime import datetime, timezone
    from arb.models import Quote
    return Quote(
        event_key="ev", venue=venue, book=venue, market_type="WINNER", line=None,
        outcome_label="home", side=side, raw_price=Decimal(cost), cost=Decimal(cost),
        fee_cost=Decimal("0"), depth_usd=Decimal("10"), ladder_json=ladder, live=False,
        fetched_at=datetime.now(timezone.utc), bid=Decimal(bid), venue_market_id=ticker)


def _opp(legs, edge):
    from arb.models import Event, Opportunity
    return Opportunity(kind="XSPREAD", category="sports",
                       event=Event(event_key="ev", category="sports", sport="mlb",
                                   home="h", away="a", start_utc=None,
                                   start_grade="nominal"),
                       legs=legs, edge=Decimal(edge), stakes=[], status="OK", notes="")


# ---- 1. the inversion -------------------------------------------------------

def test_yes_ask_ladder_inverts_the_no_bid_side():
    """Best NO bid in the fixture is 0.43, so the best YES ask is 0.57."""
    lad = kalshi.ask_ladder(OB, "YES")
    assert lad[0][0] == Decimal("0.5700"), lad[:3]
    assert lad[0][1] == Decimal("28296.79")          # size rides with its level
    assert [p for p, _ in lad] == sorted(p for p, _ in lad)   # best ask first
    assert len(lad) == len(OB["no_dollars"])


def test_no_ask_ladder_inverts_the_yes_bid_side():
    """Best YES bid is 0.56, so the best NO ask is 0.44."""
    lad = kalshi.ask_ladder(OB, "NO")
    assert lad[0][0] == Decimal("0.4400"), lad[:3]
    assert len(lad) == len(OB["yes_dollars"])


def test_ask_ladder_survives_an_empty_book():
    assert kalshi.ask_ladder({"yes_dollars": [], "no_dollars": []}, "YES") == []


def test_ladder_roundtrips_through_the_stored_form():
    """What we persist must parse back to what we computed — sizing.parse_ladder
    is what every downstream depth consumer actually calls."""
    from arb import sizing
    lad = kalshi.ask_ladder(OB, "YES")
    assert sizing.parse_ladder(kalshi.ladder_json(lad)) == lad


# ---- 2. the gate ------------------------------------------------------------

def test_gate_passing_opportunity_gets_its_kalshi_ladder():
    # half-spread sum = 0.005 + 0.005 = 0.01, edge 0.02 -> clears
    legs = [_q("kalshi", "YES", "0.50", "0.49"), _q("polymarket", "NO", "0.48", "0.47")]
    opp = _opp(legs, "0.02")
    calls = []

    def fake(cfg, ticker):
        calls.append(ticker)
        return OB

    n = cycle.enrich_gate_ladders(CFG, [opp], fetch=fake)
    assert calls == ["KXTEST"], calls
    assert n == 1
    assert legs[0].ladder_json is not None
    assert legs[1].ladder_json is None          # polymarket leg untouched


def test_gate_failing_opportunity_spends_no_request():
    """Spread 0.10 against edge 0.02 — this is the ~100x cost guard."""
    legs = [_q("kalshi", "YES", "0.50", "0.40"), _q("polymarket", "NO", "0.48", "0.47")]
    opp = _opp(legs, "0.02")
    calls = []

    def fake(cfg, ticker):
        calls.append(ticker)
        return OB

    assert cycle.enrich_gate_ladders(CFG, [opp], fetch=fake) == 0
    assert calls == []
    assert legs[0].ladder_json is None


def test_a_leg_with_no_bid_is_not_fetched():
    """No bid means no exit and no computable spread — covers.half_spread_sum returns
    None there, and an unpriceable cover must not spend a request."""
    legs = [_q("kalshi", "YES", "0.50", "0.49"), _q("polymarket", "NO", "0.48", "0.47")]
    legs[1].bid = None
    calls = []
    assert cycle.enrich_gate_ladders(CFG, [_opp(legs, "0.02")],
                                     fetch=lambda c, t: calls.append(t) or OB) == 0
    assert calls == []


def test_fetch_failure_leaves_the_ladder_null_and_does_not_raise():
    """A venue hiccup must not take the poll down; the ladder simply stays
    absent, which the depth gate already treats as INSUFFICIENT."""
    legs = [_q("kalshi", "YES", "0.50", "0.49"), _q("polymarket", "NO", "0.48", "0.47")]

    def boom(cfg, ticker):
        raise RuntimeError("kalshi 503")

    assert cycle.enrich_gate_ladders(CFG, [_opp(legs, "0.02")], fetch=boom) == 0
    assert legs[0].ladder_json is None


def test_half_spread_sum_is_public_and_matches_the_covers_gate():
    """cycle and covers must apply the SAME spread arithmetic — if these ever
    diverge the scan fetches for one population and the report scores another."""
    legs = [{"cost": "0.50", "bid": "0.49"}, {"cost": "0.48", "bid": "0.47"}]
    assert covers.half_spread_sum(legs) == pytest.approx(0.01)
    assert covers.half_spread_sum([{"cost": "0.5", "bid": None}]) is None
