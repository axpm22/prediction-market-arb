import json
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
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
    assert royals.depth_usd == Decimal("1146")


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
