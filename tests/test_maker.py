"""Maker-fill simulator. Pure — no sockets, no DB, no clock.

The whole point is the BRACKET: we cannot observe our queue position, so a
single fill number is a claim we cannot support. Conservative assumes we are
last in queue; optimistic assumes first.
"""
from decimal import Decimal

from arb.maker import MakerSim


def _sim(fee="0"):
    return MakerSim(fee_rate=Decimal(fee))


def test_no_fill_before_anything_trades_at_our_price():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    assert s.observe("tokA", Decimal("0.40"), Decimal("0"), at=1.0) == []


def test_optimistic_fills_on_the_first_size_at_our_price():
    """Front of queue: the first contract traded at our price is ours."""
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    fills = s.observe("tokA", Decimal("0.40"), Decimal("10"), at=1.0)
    assert [f["basis"] for f in fills] == ["optimistic"]
    assert fills[0]["price"] == Decimal("0.40")


def test_conservative_waits_until_the_whole_queue_ahead_is_consumed():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    s.observe("tokA", Decimal("0.40"), Decimal("60"), at=1.0)
    assert s.observe("tokA", Decimal("0.40"), Decimal("30"), at=2.0) == []
    fills = s.observe("tokA", Decimal("0.40"), Decimal("20"), at=3.0)
    assert [f["basis"] for f in fills] == ["conservative"]
    assert fills[0]["at"] == 3.0


def test_each_basis_fills_at_most_once():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("1"), posted_at=0.0)
    first = s.observe("tokA", Decimal("0.40"), Decimal("500"), at=1.0)
    assert sorted(f["basis"] for f in first) == ["conservative", "optimistic"]
    assert s.observe("tokA", Decimal("0.40"), Decimal("500"), at=2.0) == []


def test_size_at_a_worse_price_never_fills_our_bid():
    """We bid 0.40. Trades at 0.41 are someone else's; only 0.40-or-better
    reaches our resting buy."""
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("0"), posted_at=0.0)
    assert s.observe("tokA", Decimal("0.41"), Decimal("999"), at=1.0) == []
    assert s.observe("tokA", Decimal("0.40"), Decimal("1"), at=2.0) != []


def test_another_token_never_fills_our_order():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("0"), posted_at=0.0)
    assert s.observe("tokB", Decimal("0.40"), Decimal("999"), at=1.0) == []


def test_markout_marks_against_an_executable_exit_never_a_mid():
    """Measurement rule 1: mids are banned. A maker BUY exits by selling into
    the bid, so the bid is the honest mark."""
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "conservative"}
    got = markout(fill, exit_bid=Decimal("0.43"), fee=Decimal("0.005"))
    assert got == Decimal("0.025")           # 0.43 - 0.40 - 0.005


def test_markout_is_negative_when_the_book_moves_against_the_fill():
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "conservative"}
    assert markout(fill, exit_bid=Decimal("0.37"),
                   fee=Decimal("0.005")) == Decimal("-0.035")


def test_fee_is_charged_on_every_fill():
    """A maker rebate is not free money — the fee still has to clear."""
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "optimistic"}
    assert markout(fill, exit_bid=Decimal("0.40"),
                   fee=Decimal("0.01")) == Decimal("-0.01")


def test_bracket_reports_both_bounds_and_names_the_primary():
    from arb.maker import bracket
    fills = [{"basis": "conservative", "markout": Decimal("-0.01")},
             {"basis": "conservative", "markout": Decimal("0.03")},
             {"basis": "optimistic", "markout": Decimal("0.05")}]
    b = bracket(fills)
    assert b["conservative"]["n"] == 2
    assert b["conservative"]["mean"] == Decimal("0.01")
    assert b["optimistic"]["n"] == 1
    assert b["primary"] == "conservative"


def test_bracket_on_no_fills_is_empty_not_zero():
    """Zero mean from zero fills reads as 'measured, no edge'. It is not
    measured at all."""
    from arb.maker import bracket
    b = bracket([])
    assert b["conservative"]["n"] == 0
    assert b["conservative"]["mean"] is None
