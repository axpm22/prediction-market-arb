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


# --- scale-in ---------------------------------------------------------------

class _FakeStore:
    """Minimal Store-like reader returning canned later sightings."""
    def __init__(self, rows): self.rows = rows
    def query(self, sql, params=()):
        return [{"started_at": t, "edge": e} for t, e in self.rows]


def _pos(edge="0.01", hold_h=48):
    return Position(key="pk", entry=T0, exit=T0 + timedelta(hours=hold_h),
                    edge=Decimal(edge), cap_usd=Decimal("100"), cap_imputed=False,
                    confirmed=True)


def _cfg(**over):
    from arb.config import load
    from pathlib import Path
    c = load(Path(__file__).resolve().parent.parent / "config.yaml")
    c["scale_in"] = {**c["scale_in"], **over}
    return c


def test_a_frozen_edge_is_never_added_to():
    """54% of arbs visible 5+ polls never change their edge across dozens of
    consecutive polls — that is a venue serving a cached price. Adding to it
    re-buys a ghost."""
    from arb.banksim import scale_in_tranches
    same = [( (T0 + timedelta(hours=i)).isoformat(), "0.01") for i in range(1, 6)]
    tr = scale_in_tranches(_cfg(), _FakeStore(same), [_pos("0.01")])
    assert len(tr) == 1, "persistence alone must not trigger an add"


def test_present_but_not_better_is_not_added_to():
    from arb.banksim import scale_in_tranches
    rows = [((T0 + timedelta(hours=1)).isoformat(), "0.011")]   # only 1.1x
    tr = scale_in_tranches(_cfg(min_edge_widening=Decimal("1.2")),
                           _FakeStore(rows), [_pos("0.01")])
    assert len(tr) == 1


def test_a_widening_edge_is_added_to():
    from arb.banksim import scale_in_tranches
    rows = [((T0 + timedelta(hours=1)).isoformat(), "0.05")]    # 5x
    tr = scale_in_tranches(_cfg(), _FakeStore(rows), [_pos("0.01")])
    assert len(tr) == 2
    assert tr[1].n == 1 and tr[1].edge == Decimal("0.05")


def test_adds_stop_at_max_tranches():
    from arb.banksim import scale_in_tranches
    rows = [((T0 + timedelta(hours=i)).isoformat(), str(0.01 * (i + 2)))
            for i in range(1, 8)]
    tr = scale_in_tranches(_cfg(max_tranches=3), _FakeStore(rows), [_pos("0.01")])
    assert len(tr) == 3


def test_total_exposure_per_position_key_is_capped():
    """tracker.max_reappearances is 10; without a per-position cap one stale
    arb could take ten tranches of the whole bankroll."""
    from arb.banksim import Tranche
    tr = [Tranche("same", T0 + timedelta(hours=i), T0 + timedelta(hours=99),
                  Decimal("0.05"), Decimal("1000"), False, True, i)
          for i in range(4)]
    r = simulate(tr, Decimal(1000), Decimal(1), max_position_frac=Decimal("0.5"))
    assert r["deployed"] <= Decimal("500")
    assert r["capped_by_position"] >= 1
