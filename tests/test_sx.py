import json
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.pricing import q6
from arb.venues.sx import SxAdapter, parse_sx_payload, taker_cost_from_percentage_odds

FIXTURE = Path("tests/fixtures/sx_baseball_mlb.json")


def test_sx_taker_cost_inverts_maker_percentage_odds() -> None:
    assert taker_cost_from_percentage_odds("61000000000000000000") == Decimal("0.390000")


def test_sx_recorded_fixture_taker_cost_exact() -> None:
    # CLAUDE.md rule 10: best maker order on outcome two in the recorded KC@TEX fixture
    # quotes 63.5%, so the taker's cost for outcome one (Royals) is 1 - 0.635.
    adapter = SxAdapter(load_config(), Path("tests/fixtures"))
    quotes = adapter.fetch_quotes("baseball_mlb")
    royals = next(q for q in quotes if q.market_type == "WINNER" and q.outcome_label == "kansas city royals")
    assert royals.cost == Decimal("0.365000")
    # weakness#10: taker $-notional = maker_stake(1146) * c/(1-c) = 1146 * 0.365/0.635.
    assert royals.depth_usd == Decimal("658.724409")


def test_sx_fixture_parses_winner_and_totals() -> None:
    adapter = SxAdapter(load_config(), Path("tests/fixtures"))
    quotes = adapter.fetch_quotes("baseball_mlb")
    winners = {q.outcome_label for q in quotes if q.market_type == "WINNER"}
    totals = [(q.outcome_label, q.line) for q in quotes if q.market_type == "TOTAL"]
    assert winners == {"kansas city royals", "texas rangers"}
    assert {label for label, _ in totals} == {"OVER", "UNDER"}
    assert all(line == Decimal("11.5") for _, line in totals)
    assert {q.event_key for q in quotes} == {"baseball_mlb:2026-06-10:kansas city royals:texas rangers"}


def test_sx_quotes_flag_unverified_fee_only_while_rate_is_null() -> None:
    payload = json.loads(FIXTURE.read_text())
    unverified = parse_sx_payload(payload, "baseball_mlb", fee_rate=None)
    assert all("fee-rate-unverified" in q.notes for q in unverified)
    verified = parse_sx_payload(payload, "baseball_mlb", fee_rate=Decimal("0"))
    assert all("fee-rate-unverified" not in q.notes for q in verified)
    assert all("settlement-rule-review" in q.notes for q in verified)


def test_sx_winning_bet_fee_is_charged_on_the_leg() -> None:
    # D3 (ARB_LADDER_AND_FRICTIONS_SPEC §6): an arb always has one winning leg, and SX charges
    # only on winnings, so a winning SX leg pays win_fee_rate*(1-cost). At 0 the fee is unchanged;
    # at 0.05 every SX leg's fee_cost rises by exactly q6(0.05*(1-cost)) and is flagged.
    payload = json.loads(FIXTURE.read_text())
    base = parse_sx_payload(payload, "baseball_mlb", fee_rate=Decimal("0"))
    winfee = parse_sx_payload(payload, "baseball_mlb", fee_rate=Decimal("0"), win_fee_rate=Decimal("0.05"))
    assert base and len(base) == len(winfee)
    assert all("sx-win-fee-modeled" not in q.notes for q in base)
    assert all("sx-win-fee-modeled" in q.notes for q in winfee)
    for b, w in zip(base, winfee):
        assert w.fee_cost == b.fee_cost + q6(Decimal("0.05") * (Decimal("1") - b.cost))
        assert w.effective_cost > b.effective_cost  # the SX leg got more expensive → arbs can drop
