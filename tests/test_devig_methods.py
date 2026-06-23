from decimal import Decimal

from arb.pricing import devig_book, devig_book_method, devig_book_power, devig_book_shin


def _odds(*pairs):
    return {name: Decimal(o) for name, o in pairs}


def test_symmetric_book_is_half_half_for_all_methods() -> None:
    book = _odds(("a", "1.91"), ("b", "1.91"))
    for fn in (devig_book, devig_book_power, devig_book_shin):
        out = fn(book)
        assert out["a"] == Decimal("0.500000") and out["b"] == Decimal("0.500000")


def test_proportional_vector_unchanged() -> None:
    out = devig_book(_odds(("fav", "1.50"), ("mid", "2.86"), ("dog", "6.00")))
    # ~(0.5635, 0.2956, 0.1409) per spec; largest carries the rounding residual.
    assert out["fav"] == Decimal("0.563546")
    assert out["dog"] == Decimal("0.140887")
    assert sum(out.values()) == Decimal("1.000000")


def test_power_and_shin_downweight_the_longshot() -> None:
    book = _odds(("fav", "1.50"), ("mid", "2.86"), ("dog", "6.00"))
    prop = devig_book(book)["dog"]
    assert devig_book_power(book)["dog"] < prop   # longshot gets LESS prob than proportional
    assert devig_book_shin(book)["dog"] < prop
    # ...and the favorite correspondingly gets more.
    assert devig_book_power(book)["fav"] > devig_book(book)["fav"]


def test_all_methods_sum_to_one() -> None:
    book = _odds(("a", "1.30"), ("b", "4.50"), ("c", "11.0"))
    for method in ("proportional", "power", "shin"):
        out = devig_book_method(book, method)
        assert sum(out.values()) == Decimal("1.000000")


def test_dispatcher_falls_back_to_proportional_on_unknown() -> None:
    book = _odds(("a", "1.91"), ("b", "1.91"))
    assert devig_book_method(book, "nonsense") == devig_book(book)
