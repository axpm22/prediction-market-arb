"""Fee model vectors (SPEC §6.5, §7.2). Single leg_fee_cost implementation."""
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.venues.fees import ceil_to_cent, fee_is_stale, leg_fee_cost

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")


def test_kalshi_money_vector_execution_mode():
    # 10 contracts @ P=0.40 -> ceil_to_cent(0.07*10*0.40*0.60 = 0.168) = $0.17
    fee = leg_fee_cost(CFG, "kalshi", "sports", Decimal("0.40"), Decimal("10"),
                       mode="execution")
    assert fee == Decimal("0.17")


def test_kalshi_paper_mode_is_continuous():
    # paper-ledger fees use the continuous curve — no cent ceiling (§7.2)
    fee = leg_fee_cost(CFG, "kalshi", "sports", Decimal("0.50"), Decimal("1"))
    assert fee == Decimal("0.0175")


def test_polymarket_quadratic_vector():
    # p=0.50, sports 0.03 -> 0.0075; the linear model's 0.015 was the double-charge bug
    fee = leg_fee_cost(CFG, "polymarket", "sports", Decimal("0.50"), Decimal("1"))
    assert fee == Decimal("0.0075")
    assert fee != Decimal("0.015")


def test_polymarket_category_rates():
    p, c = Decimal("0.50"), Decimal("1")
    assert leg_fee_cost(CFG, "polymarket", "crypto", p, c) == Decimal("0.0175")
    assert leg_fee_cost(CFG, "polymarket", "politics", p, c) == Decimal("0.01")
    assert leg_fee_cost(CFG, "polymarket", "weather", p, c) == Decimal("0.0125")
    assert leg_fee_cost(CFG, "polymarket", "geopolitics", p, c) == Decimal("0")


def test_sx_singles_free():
    assert leg_fee_cost(CFG, "sx", "sports", Decimal("0.50"), Decimal("1")) == Decimal("0")


def test_maker_rates_kalshi_reduced_not_zero():
    # Kalshi maker = 25% of taker, NEVER zero (§2 item 4)
    taker = leg_fee_cost(CFG, "kalshi", "sports", Decimal("0.50"), Decimal("1"))
    maker = leg_fee_cost(CFG, "kalshi", "sports", Decimal("0.50"), Decimal("1"), role="maker")
    assert maker == taker * Decimal("0.25")
    assert maker > 0
    assert leg_fee_cost(CFG, "polymarket", "sports", Decimal("0.5"), Decimal("1"),
                        role="maker") == Decimal("0")


def test_ceil_to_cent():
    assert ceil_to_cent(Decimal("0.168")) == Decimal("0.17")
    assert ceil_to_cent(Decimal("0.17")) == Decimal("0.17")
    assert ceil_to_cent(Decimal("0.1701")) == Decimal("0.18")


def test_unknown_venue_is_error():
    with pytest.raises(KeyError):
        leg_fee_cost(CFG, "bovada", "sports", Decimal("0.5"), Decimal("1"))


def test_fee_staleness_is_dated_fact():
    import datetime
    assert not fee_is_stale(CFG, "kalshi", datetime.date(2026, 7, 8))
    assert fee_is_stale(CFG, "kalshi", datetime.date(2026, 9, 1))


def test_pm_tag_category_covers_config_tags():
    # every configured negrisk/nonsports tag must map to a REAL fee category —
    # cmd_negrisk_scan used to price every basket as "politics" (wrong rate for
    # economy/weather)
    from arb.venues.fees import _PM_CATEGORY_RATE_KEY, PM_TAG_CATEGORY
    for tag in CFG["nonsports"]["polymarket_tags"]:
        cat = PM_TAG_CATEGORY[str(tag)]
        assert cat in _PM_CATEGORY_RATE_KEY
    # economy must hit the econ/weather rate, not the politics rate
    econ = leg_fee_cost(CFG, "polymarket", PM_TAG_CATEGORY["economy"], Decimal("0.5"))
    pol = leg_fee_cost(CFG, "polymarket", "politics", Decimal("0.5"))
    assert econ != pol


def test_kalshi_series_multiplier_halves_fee():
    from decimal import Decimal
    from arb.venues.fees import leg_fee_cost
    cfg = {"kalshi": {"fee_rate": Decimal("0.07"), "maker_fee_rate": Decimal("0.0175")}}
    full = leg_fee_cost(cfg, "kalshi", "sports", Decimal("0.40"), Decimal(10), mode="execution")
    half = leg_fee_cost(cfg, "kalshi", "sports", Decimal("0.40"), Decimal(10), mode="execution",
                        multiplier=Decimal("0.5"))
    assert full == Decimal("0.17") and half == Decimal("0.09")  # 0.084 ceils to 0.09
