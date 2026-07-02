"""Stage-1 deterministic strike matcher (STRIKE_AWARE_NONSPORTS_MATCHER): parser vectors +
exact-alignment safety. Pure functions, no network."""
from datetime import date
from decimal import Decimal

import pytest

from arb.experimental.strike_match import (
    GE, GT, LE, LT, StrikeSpec, aligned, kalshi_strike, parse_strike, strike_candidates,
)


@pytest.mark.parametrize("text, kind, comp, value, unit", [
    (">=100°F", "threshold", GE, Decimal("100"), "F"),
    ("100° or above", "threshold", GE, Decimal("100"), "deg"),
    ("106° or above", "threshold", GE, Decimal("106"), "deg"),
    ("97° or below", "threshold", LE, Decimal("97"), "deg"),
    ("at least 3500", "threshold", GE, Decimal("3500"), "count"),
    ("less than 4m square kilometers", "threshold", LT, Decimal("4"), "count"),
    (">= 50 bps", "threshold", GE, Decimal("50"), "bps"),
    ("at least 3%", "threshold", GE, Decimal("3"), "pct"),
    ("38°C or above", "threshold", GE, Decimal("38"), "C"),
])
def test_threshold_vectors(text, kind, comp, value, unit) -> None:
    spec = parse_strike(text)
    assert spec is not None and spec.kind == kind
    assert spec.comparator == comp and spec.value == value and spec.unit == unit


@pytest.mark.parametrize("text, low, high, low_inc, high_inc", [
    ("between 96-97°", Decimal("96"), Decimal("97"), True, True),
    ("[70,75)", Decimal("70"), Decimal("75"), True, False),
    ("[70,72.5)", Decimal("70"), Decimal("72.5"), True, False),
    ("between 4.0 & 4.2m square kilometers", Decimal("4.0"), Decimal("4.2"), True, False),
])
def test_range_vectors(text, low, high, low_inc, high_inc) -> None:
    spec = parse_strike(text)
    assert spec is not None and spec.kind == "range"
    assert spec.low == low and spec.high == high
    assert spec.low_inclusive == low_inc and spec.high_inclusive == high_inc


@pytest.mark.parametrize("text", [
    "Will it rain in NYC tomorrow?",
    "Who wins the presidential election?",
    "Will the Fed chair resign this year?",
])
def test_unparseable_returns_none(text) -> None:
    assert parse_strike(text) is None


def test_dual_unit_conversion_aligns_only_when_stated() -> None:
    stated = parse_strike("100°F (37.8°C) or above")     # dual-unit
    plain_f = parse_strike(">=100°F")
    assert stated.dual_unit and aligned(stated, plain_f)
    # A bare °C spec (no °F stated) must NOT align with °F even if ~equal — unit mismatch.
    bare_c = parse_strike("38°C or above")
    assert not aligned(bare_c, plain_f)


def test_identical_buckets_align_off_by_half_width_do_not() -> None:
    a = parse_strike("[70,75)")
    assert aligned(a, parse_strike("[70,75)"))
    assert not aligned(a, parse_strike("[70,72.5)"))       # half-width high bound


def test_comparator_and_value_strictness() -> None:
    assert not aligned(parse_strike("> 99"), parse_strike(">= 100"))   # int-equiv, not aligned
    assert not aligned(parse_strike("> 100"), parse_strike(">= 100"))  # comparator mismatch
    assert aligned(parse_strike("> 100"), parse_strike("> 100"))


def test_kalshi_structured_strike() -> None:
    less = kalshi_strike({"strike_type": "less", "cap_strike": 98,
                          "title": "high temp in NYC be <98°", "yes_sub_title": "97° or below"})
    assert less == StrikeSpec("threshold", "deg", comparator=LT, value=Decimal("98"))
    between = kalshi_strike({"strike_type": "between", "floor_strike": 98, "cap_strike": 99,
                             "yes_sub_title": "98° to 99°"})
    assert between.kind == "range" and between.low == 98 and between.high == 99
    assert between.low_inclusive and between.high_inclusive


def test_strike_candidates_blocks_and_aligns() -> None:
    k = [{"ticker": "KXHIGHNY-26JUL03-T105", "strike_type": "greater", "floor_strike": 105,
          "title": "Will the high temp in NYC be >105° on Jul 3, 2026?",
          "yes_sub_title": "106° or above", "close_time": "2026-07-04T04:59:00Z"}]
    # Aligned Polymarket market (same date, same entity, > 105) + a misaligned one (>= 106).
    p_aligned = {"conditionId": "0xAA", "question": "Will NYC high temp be over 105° on Jul 3, 2026?",
                 "endDate": "2026-07-04T00:00:00Z", "outcomes": '["Yes","No"]',
                 "clobTokenIds": '["tokY","tokN"]'}
    p_off = {"conditionId": "0xBB", "question": "Will NYC high temp be 106° or above on Jul 3?",
             "endDate": "2026-07-04T00:00:00Z", "outcomes": '["Yes","No"]', "clobTokenIds": '["y","n"]'}
    cands = strike_candidates(k, [p_aligned, p_off], threshold=60)
    assert len(cands) == 1
    assert cands[0].kalshi_ticker == "KXHIGHNY-26JUL03-T105"
    assert cands[0].polymarket_condition_id == "0xAA" and cands[0].polymarket_token_id_yes == "tokY"


def test_strike_candidates_rejects_different_close_dates() -> None:
    k = [{"ticker": "K1", "strike_type": "greater", "floor_strike": 105,
          "title": "NYC high temp > 105 Jul 3", "close_time": "2026-07-04T04:59:00Z"}]
    p = [{"conditionId": "0xAA", "question": "NYC high temp over 105 Jul 4",
          "endDate": "2026-07-05T00:00:00Z", "outcomes": '["Yes","No"]', "clobTokenIds": '["y","n"]'}]
    assert strike_candidates(k, p, threshold=60) == []


def test_parse_gap_is_logged_not_swallowed() -> None:
    gaps: list[str] = []
    k = [{"ticker": "K1", "title": "Some unparseable weather thing", "close_time": "2026-07-04T00:00:00Z"}]
    strike_candidates(k, [], threshold=60, on_parse_gap=gaps.append)
    assert any("strike-parse-gap kalshi:K1" in g for g in gaps)
