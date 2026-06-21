import json
from decimal import Decimal
from pathlib import Path

from arb.arbitrage import VERIFIABLE_VENUES, has_unverifiable_leg
from arb.config import load_config
from arb.store import quote_to_json
from arb.venues.sx import SxAdapter, parse_sx_payload, requote_sx_leg


def test_sx_is_now_verifiable() -> None:
    assert "sx" in VERIFIABLE_VENUES


def test_sx_quotes_carry_outcome_side_ref() -> None:
    quotes = SxAdapter(load_config(), Path("tests/fixtures")).fetch_quotes("baseball_mlb")
    # outcome one and two get distinct side hints that survive JSON serialization.
    refs = {q.outcome_label: q.ref for q in quotes if q.market_type == "WINNER"}
    assert set(refs.values()) == {"1", "2"}
    leg = quote_to_json(quotes[0])
    assert leg["ref"] in {"1", "2"}


def test_sx_leg_with_ref_is_not_unverifiable() -> None:
    quotes = SxAdapter(load_config(), Path("tests/fixtures")).fetch_quotes("baseball_mlb")
    kalshi_like = quotes[0]  # any quote; pair an SX leg with itself is enough for the check
    assert not has_unverifiable_leg([q for q in quotes[:2]])  # both SX, both carry ref


def test_requote_sx_leg_reprices_the_correct_side(monkeypatch) -> None:
    # Build a fake orders payload: outcome one's taker reads maker-on-two==False orders.
    market_hash = "0xabc"
    orders = [
        {"marketHash": market_hash, "percentageOdds": "61000000000000000000",
         "isMakerBettingOutcomeOne": False, "totalBetSize": "100000000", "fillAmount": "0"},
        {"marketHash": market_hash, "percentageOdds": "52000000000000000000",
         "isMakerBettingOutcomeOne": True, "totalBetSize": "75000000", "fillAmount": "0"},
    ]

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": orders}

    class FakeSession:
        headers = {}
        def get(self, *a, **k): return FakeResp()

    # ref "1" → outcome one → taker cost = 1 - 0.61 = 0.39 (fee 0 for SX).
    cost_one = requote_sx_leg(market_hash, "1", Decimal("0"), session=FakeSession())
    assert cost_one == Decimal("0.390000")
    # ref "2" → outcome two → taker cost = 1 - 0.52 = 0.48.
    cost_two = requote_sx_leg(market_hash, "2", Decimal("0"), session=FakeSession())
    assert cost_two == Decimal("0.480000")
    # missing ref → cannot reprice.
    assert requote_sx_leg(market_hash, None, Decimal("0"), session=FakeSession()) is None
