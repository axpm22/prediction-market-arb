"""Bankroll-constrained paper book. Synthetic positions only — no DB, no clock."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.banksim import Position, simulate

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _p(i, edge="0.02", cap="100", hold_h=48, confirmed=True):
    return Position(key=f"k{i}", entry=T0 + timedelta(hours=i),
                    exit=T0 + timedelta(hours=i + hold_h), edge=Decimal(edge),
                    cap_usd=Decimal(cap), cap_imputed=False, confirmed=confirmed)


def test_capital_is_released_before_the_next_arrival_is_sized():
    """Releasing after sizing understates the pot badly — the first cut of this
    never returned capital at all and reported 4x too few fills."""
    a = _p(0, hold_h=1, cap="100")          # frees at T0+1h
    b = _p(2, cap="100")                    # arrives T0+2h, after a settled
    r = simulate([a, b], Decimal(100), Decimal(1))
    assert r["taken"] == 2, "second arb must reuse the freed capital"
    assert r["missed_capital"] == 0


def test_a_miss_for_capital_is_not_a_miss_for_quality():
    """Two overlapping arbs, a pot that funds one. The second is missed for
    capital and must be counted separately from anything about its merit."""
    r = simulate([_p(0, cap="100"), _p(1, cap="100")], Decimal(100), Decimal(1))
    assert r["taken"] == 1
    assert r["missed_capital"] == 1


def test_position_cap_binds_below_the_bankroll():
    r = simulate([_p(0, cap="30")], Decimal(1000), Decimal(1))
    assert r["deployed"] == Decimal("30")


def test_per_position_fraction_binds_below_the_cap():
    r = simulate([_p(0, cap="1000")], Decimal(1000), Decimal("0.25"))
    assert r["deployed"] == Decimal("250")


def test_pnl_is_stake_times_edge():
    r = simulate([_p(0, edge="0.05", cap="200")], Decimal(1000), Decimal(1))
    assert r["pnl"] == Decimal("10.000000")


def test_confirmed_only_mode_skips_flags_that_ran_away():
    """A flag that never confirmed is one that vanished before we could act;
    funding it in the headline would book profit on a fill nobody could get."""
    pos = [_p(0, cap="100", confirmed=False), _p(5, cap="100", confirmed=True)]
    loose = simulate(pos, Decimal(1000), Decimal(1))
    strict = simulate(pos, Decimal(1000), Decimal(1), require_confirmed=True)
    assert loose["taken"] == 2
    assert strict["taken"] == 1 and strict["skipped_unconfirmed"] == 1


def test_bigger_bankroll_never_earns_less():
    pos = [_p(i, cap="100") for i in range(6)]
    small = simulate(pos, Decimal(100), Decimal(1))["pnl"]
    big = simulate(pos, Decimal(1000), Decimal(1))["pnl"]
    assert big >= small
