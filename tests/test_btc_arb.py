from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.btc_arb import BtcLeg, find_btc_arb, windows_match

W = datetime(2026, 6, 21, 11, 0, tzinfo=timezone.utc)


def leg(venue, direction, cost, end=W, oracle="coinbase_idx", asset="BTC"):
    return BtcLeg(venue, asset, direction, end, Decimal(cost), oracle)


def test_btc_arb_vector() -> None:
    # BUILD_PLAN Phase 4 vector: UP(A)=0.48, DOWN(B)=0.49, sum 0.97 < 1 -> arb.
    arbs = find_btc_arb([leg("kalshi", "UP", "0.48"), leg("polymarket", "DOWN", "0.49")],
                        window_match_secs=5, trusted=False)
    assert len(arbs) == 1
    assert arbs[0].total_cost == Decimal("0.970000")
    assert arbs[0].edge == (Decimal("1") / Decimal("0.97") - Decimal("1")).quantize(Decimal("0.000001"))


def test_window_mismatch_not_matched() -> None:
    # End-times 7s apart with tolerance 5 -> not the same window.
    assert not windows_match(W, W + timedelta(seconds=7), 5)
    arbs = find_btc_arb([leg("kalshi", "UP", "0.48"), leg("polymarket", "DOWN", "0.49", end=W + timedelta(seconds=7))],
                        window_match_secs=5, trusted=False)
    assert arbs == []


def test_different_oracles_force_review() -> None:
    arbs = find_btc_arb(
        [leg("kalshi", "UP", "0.48", oracle="kalshi_idx"),
         leg("polymarket", "DOWN", "0.49", oracle="polymarket_oracle")],
        window_match_secs=5, trusted=True)
    assert arbs[0].same_oracle is False
    assert arbs[0].status == "REVIEW"   # never OK when oracles differ, even if trusted


def test_same_oracle_and_trusted_is_ok() -> None:
    arbs = find_btc_arb([leg("kalshi", "UP", "0.48"), leg("polymarket", "DOWN", "0.49")],
                        window_match_secs=5, trusted=True)
    assert arbs[0].same_oracle is True
    assert arbs[0].status == "OK"


def test_same_venue_not_paired() -> None:
    assert find_btc_arb([leg("kalshi", "UP", "0.48"), leg("kalshi", "DOWN", "0.49")],
                        window_match_secs=5, trusted=False) == []
