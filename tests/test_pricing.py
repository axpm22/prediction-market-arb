from decimal import Decimal

from arb.pricing import devig_book, expected_value, kalshi_fee_cost, kalshi_fee_money, q6


def test_devig_even_two_way_book() -> None:
    probs = devig_book({"A": Decimal("1.91"), "B": Decimal("1.91")})
    assert probs == {"A": Decimal("0.500000"), "B": Decimal("0.500000")}


def test_devig_three_way_book() -> None:
    probs = devig_book({"HOME": Decimal("1.50"), "DRAW": Decimal("2.86"), "AWAY": Decimal("6.00")})
    assert abs(probs["HOME"] - Decimal("0.5635")) < Decimal("0.0001")
    assert abs(probs["DRAW"] - Decimal("0.2956")) < Decimal("0.0001")
    assert abs(probs["AWAY"] - Decimal("0.1409")) < Decimal("0.0001")
    assert q6(sum(probs.values(), Decimal("0"))) == Decimal("1.000000")


def test_kalshi_fee_and_ev_vector() -> None:
    fee_cost = kalshi_fee_cost(Decimal("0.50"), Decimal("0.07"))
    assert fee_cost == Decimal("0.017500")
    effective_cost = Decimal("0.50") + fee_cost
    assert expected_value(Decimal("0.55"), effective_cost) == Decimal("0.062802")


def test_kalshi_money_fee_rounds_up_to_cent() -> None:
    assert kalshi_fee_money(Decimal("10"), Decimal("0.40"), Decimal("0.07")) == Decimal("0.17")
