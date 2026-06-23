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


def test_sharp_book_weighting_pulls_consensus_toward_sharp_book() -> None:
    from datetime import datetime, timezone
    from arb.models import Quote
    from arb.pricing import consensus_probabilities

    def oq(book, outcome, odds):
        return Quote(event_key="e", venue="oddsapi", book=book, market_type="WINNER", line=None,
                     outcome_label=outcome, raw_price=Decimal(odds), cost=Decimal("0"),
                     fee_cost=Decimal("0"), depth_usd=None, live=False,
                     fetched_at=datetime.now(timezone.utc))
    # two soft books say ~0.50/0.50, one sharp book says fav is stronger (~0.60/0.40).
    quotes = [
        oq("soft1", "fav", "2.00"), oq("soft1", "dog", "2.00"),
        oq("soft2", "fav", "2.00"), oq("soft2", "dog", "2.00"),
        oq("lowvig", "fav", "1.55"), oq("lowvig", "dog", "2.55"),
    ]
    plain = consensus_probabilities(quotes, min_books=3)
    weighted = consensus_probabilities(quotes, min_books=3, sharp_books={"lowvig"}, sharp_weight=3)
    # weighting the sharp book up moves the median toward its (higher) fav probability.
    assert weighted["fav"] > plain["fav"]
    assert sum(weighted.values()) == Decimal("1.000000")
