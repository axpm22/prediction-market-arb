from datetime import datetime, timezone
from decimal import Decimal

from arb.models import Quote
from arb.pricing import all_in_fee_cost, consensus_probabilities, kalshi_fee_cost, linear_fee_cost


def oq(book: str, outcome: str, odds: str, market_type: str = "WINNER", line=None) -> Quote:
    return Quote(event_key="soccer:2026-06-22:a:b", venue="oddsapi", book=book,
                 market_type=market_type, line=line, outcome_label=outcome,
                 raw_price=Decimal(odds), cost=Decimal("0"), fee_cost=Decimal("0"),
                 depth_usd=None, live=False, fetched_at=datetime.now(timezone.utc))


# ---- #1 consensus renormalizes per market ----

def test_consensus_vector_sums_to_one() -> None:
    # 3 books, 2-way; medians taken independently would not necessarily sum to 1.
    quotes = []
    for i, (a, b) in enumerate([("1.90", "2.00"), ("1.95", "1.95"), ("2.10", "1.80")]):
        quotes += [oq(f"bk{i}", "home", a), oq(f"bk{i}", "away", b)]
    cons = consensus_probabilities(quotes, min_books=3)
    assert set(cons) == {"home", "away"}
    assert cons["home"] + cons["away"] == Decimal("1.000000")


# ---- #2 incomplete books don't vote ----

def test_incomplete_soccer_book_excluded() -> None:
    # 3 complete 3-way books + 2 books missing DRAW. The 2-way books must NOT inflate.
    quotes = []
    for i in range(3):
        quotes += [oq(f"full{i}", "home", "2.5"), oq(f"full{i}", "draw", "3.3"), oq(f"full{i}", "away", "2.9")]
    for i in range(2):
        quotes += [oq(f"half{i}", "home", "1.8"), oq(f"half{i}", "away", "2.0")]  # no draw
    cons = consensus_probabilities(quotes, min_books=3)
    assert set(cons) == {"home", "draw", "away"}     # 3-way market resolved
    assert cons["home"] + cons["draw"] + cons["away"] == Decimal("1.000000")
    # draw ~ 1/3.3 normalized; if the 2-way books had voted, home would be badly inflated.
    assert cons["draw"] > Decimal("0.25")


def test_market_below_min_books_excluded() -> None:
    quotes = [oq("bk0", "home", "1.9"), oq("bk0", "away", "2.0")]  # only 1 book
    assert consensus_probabilities(quotes, min_books=3) == {}


# ---- #5 per-venue fee basis ----

def test_kalshi_quadratic_vs_polymarket_linear_differ() -> None:
    p, rate = Decimal("0.50"), Decimal("0.03")
    assert kalshi_fee_cost(p, rate) == Decimal("0.007500")    # 0.03*0.5*0.5
    assert linear_fee_cost(p, rate) == Decimal("0.015000")    # 0.03*0.5
    # all_in routes by basis; the bug was Polymarket getting the quadratic curve.
    assert all_in_fee_cost(p, rate, basis="quadratic") == Decimal("0.007500")
    assert all_in_fee_cost(p, rate, basis="linear") == Decimal("0.015000")


def test_linear_fee_not_max_at_half() -> None:
    # Linear keeps rising with price; quadratic peaks at 0.5. At p=0.9 they diverge sharply.
    rate = Decimal("0.03")
    assert linear_fee_cost(Decimal("0.90"), rate) == Decimal("0.027000")
    assert kalshi_fee_cost(Decimal("0.90"), rate) == Decimal("0.002700")


# ---- #3 totals use a lower book threshold (line-fragmented) ----

def test_totals_consensus_uses_lower_threshold() -> None:
    # 2 books on the SAME total line 8.5. With min_books=3 totals would be dead;
    # min_books_totals=2 recovers it (winner markets still need 3).
    quotes = []
    for i in range(2):
        quotes += [oq(f"t{i}", "OVER", "1.95", "TOTAL", line=Decimal("8.5")),
                   oq(f"t{i}", "UNDER", "1.95", "TOTAL", line=Decimal("8.5"))]
    assert consensus_probabilities(quotes, min_books=3) == {}             # totals dead at 3
    cons = consensus_probabilities(quotes, min_books=3, min_books_totals=2)
    assert set(cons) == {"OVER:8.5", "UNDER:8.5"}
    assert cons["OVER:8.5"] + cons["UNDER:8.5"] == Decimal("1.000000")    # still renormalized


# ---- #4 event_key has no boundary-splitting bucket ----

def test_event_key_no_sub_day_bucket() -> None:
    from datetime import datetime, timezone
    from arb.matching import event_key
    a = event_key("baseball_mlb", "Texas", "Kansas City",
                  datetime(2026, 6, 10, 17, 55, tzinfo=timezone.utc))
    b = event_key("baseball_mlb", "Texas", "Kansas City",
                  datetime(2026, 6, 10, 18, 5, tzinfo=timezone.utc))
    assert a == b   # 17:55 vs 18:05 (straddling the old 18:00 bucket boundary) now group
    assert a.count(":") == 3   # sport:date:away:home, no trailing bucket
