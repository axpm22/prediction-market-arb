"""Strategy probes (S-A transient PM coherence, S-B achievable skew).
Pure state machine only — no sockets, no venue calls in tests (rule 2 ethos)."""
from decimal import Decimal

from arb.probe import CoherenceState, _best, skew_stats

YES, NO = "tokYES", "tokNO"


def _frame(aid, ask, size=100, bid=None):
    return {"asset_id": aid,
            "asks": [{"price": str(ask), "size": str(size)}],
            "bids": [{"price": str(bid), "size": str(size)}] if bid else [],
            "timestamp": "1"}


def test_best_ignores_empty_book_sentinels():
    """0.001-floor and >=1 prices are the empty-book pattern already known to
    fake covers elsewhere in this codebase; they are not quotes."""
    assert _best([{"price": "0", "size": "5"}], "asks") is None
    assert _best([{"price": "1", "size": "5"}], "asks") is None
    assert _best([{"price": "0.4", "size": "0"}], "asks") is None
    assert _best([{"price": "0.4", "size": "5"},
                  {"price": "0.3", "size": "5"}], "asks") == (Decimal("0.3"), Decimal("5"))
    assert _best([{"price": "0.4", "size": "5"},
                  {"price": "0.3", "size": "5"}], "bids") == (Decimal("0.4"), Decimal("5"))


def test_no_cover_reported_until_both_sides_are_known():
    """One live book and one stale/absent book is not a cover — that is the
    single-leg mistake, priced as if the other side were free."""
    s = CoherenceState(YES, NO)
    assert s.apply(_frame(YES, "0.10")) is None
    assert s.summary()["covers"] == 0


def test_sum_below_one_is_a_cover_with_size_and_edge():
    s = CoherenceState(YES, NO)
    s.apply(_frame(YES, "0.40", size=250))
    hit = s.apply(_frame(NO, "0.55", size=120))
    assert hit is not None
    assert hit["sum"] == Decimal("0.95")
    assert hit["edge"] == Decimal("0.05")
    assert hit["size"] == Decimal("120")          # the SMALLER leg bounds size
    assert hit["notional"] == Decimal("120") * Decimal("0.95")


def test_sum_at_or_above_one_is_not_a_cover():
    s = CoherenceState(YES, NO)
    s.apply(_frame(YES, "0.50"))
    assert s.apply(_frame(NO, "0.50")) is None    # exactly 1.00 pays nothing
    assert s.apply(_frame(NO, "0.51")) is None
    assert s.summary()["best_sum"] == Decimal("1.00")


def test_fees_are_charged_before_a_cover_is_claimed():
    """0.99 gross looks like a point of edge; at a 2% taker fee it costs
    1.0098 and is not a cover at all. Reporting the gross number is how a fake
    arb gets booked — the fee has to flip the verdict, not just shade it."""
    free = CoherenceState(YES, NO)
    free.apply(_frame(YES, "0.495"))
    assert free.apply(_frame(NO, "0.495")) is not None      # 0.99 gross

    s = CoherenceState(YES, NO, fee_rate=Decimal("0.02"))
    s.apply(_frame(YES, "0.495"))
    assert s.apply(_frame(NO, "0.495")) is None             # 0.99 + 0.0198 > 1


def test_stale_side_is_reused_so_a_one_sided_update_still_evaluates():
    """WS sends per-token deltas. If the NO book is only reported once, the
    YES updates that follow must still be checked against it."""
    s = CoherenceState(YES, NO)
    s.apply(_frame(NO, "0.55"))
    assert s.apply(_frame(YES, "0.60")) is None   # 1.15
    hit = s.apply(_frame(YES, "0.40"))            # NO reused
    assert hit is not None and hit["sum"] == Decimal("0.95")


def test_frames_for_other_assets_are_ignored():
    s = CoherenceState(YES, NO)
    assert s.apply(_frame("someone-else", "0.01")) is None
    assert s.summary()["updates"] == 0


def test_skew_stats_reports_the_distribution_not_just_a_mean():
    assert skew_stats([]) == {"n": 0}
    r = skew_stats([(0.0, 1.0), (0.0, 2.0), (0.0, 9.0)])
    assert r["n"] == 3 and r["min"] == 1.0 and r["median"] == 2.0
    assert r["max"] == 9.0


# Verbatim shape from the live feed, 2026-07-30. price_changes[] (NOT
# changes[]), asset_id per CHANGE not on the frame root, and best_bid/best_ask
# stated on each change. Parsing it the other way registered 4 updates out of
# 935 observed, which would have made "0 covers" meaningless.
def _pc(items):
    return {"event_type": "price_change", "timestamp": "1785457066528",
            "price_changes": items}


def test_price_change_frames_are_parsed_from_price_changes_not_changes():
    s = CoherenceState(YES, NO)
    s.feed_ws(_pc([{"asset_id": YES, "price": "0.37", "size": "405",
                    "side": "SELL", "best_bid": "0.39", "best_ask": "0.40"}]))
    assert s.summary()["updates"] == 1, "per-change asset_id must be read"
    s.feed_ws(_pc([{"asset_id": NO, "price": "0.63", "size": "405",
                    "side": "BUY", "best_bid": "0.58", "best_ask": "0.59"}]))
    assert s.summary()["best_sum"] == Decimal("0.99")


def test_one_frame_can_carry_both_legs_and_produce_a_cover():
    s = CoherenceState(YES, NO)
    hits = s.feed_ws(_pc([
        {"asset_id": YES, "best_bid": "0.39", "best_ask": "0.40"},
        {"asset_id": NO, "best_bid": "0.55", "best_ask": "0.56"}]))
    assert len(hits) == 1 and hits[0]["sum"] == Decimal("0.96")


def test_resolved_book_touches_are_ignored_not_read_as_covers():
    """A settled market sits at 0.001/0.999. Those are sentinels, and treating
    them as a touch manufactures covers out of dead books."""
    s = CoherenceState(YES, NO)
    s.feed_ws(_pc([{"asset_id": YES, "best_bid": "0", "best_ask": "0"},
                   {"asset_id": NO, "best_bid": "1", "best_ask": "1"}]))
    assert s.summary()["updates"] == 0 and s.summary()["covers"] == 0


def test_book_frames_still_work_through_feed_ws():
    s = CoherenceState(YES, NO)
    s.feed_ws({"event_type": "book", "asset_id": YES,
               "asks": [{"price": "0.40", "size": "100"}], "bids": []})
    hits = s.feed_ws({"event_type": "book", "asset_id": NO,
                      "asks": [{"price": "0.55", "size": "100"}], "bids": []})
    assert len(hits) == 1 and hits[0]["sum"] == Decimal("0.95")
