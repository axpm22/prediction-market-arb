from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.btc_arb import BtcLeg, find_btc_arb, persist_btc_arbs, windows_match
from arb.store import Store

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


def test_persist_btc_arb_review_row_is_append_only(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("btc")
    arbs = find_btc_arb([leg("kalshi", "UP", "0.48"), leg("polymarket", "DOWN", "0.49")],
                        window_match_secs=5, trusted=False)

    assert persist_btc_arbs(store, poll, arbs) == 1
    assert persist_btc_arbs(store, poll, arbs) == 0
    with store.connect() as con:
        [row] = list(con.execute("SELECT * FROM flat_bets WHERE kind='ARB_BTC'"))

    assert row["trusted"] == 0
    assert row["quality_flag"] == "oracle-review"
    assert row["optimistic_cost"] == "0.970000"
    assert row["pnl_optimistic"] == "0.03"
    assert "same-oracle" in row["notes"]


def test_persist_btc_arb_trusted_same_oracle_is_clean(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("btc")
    arbs = find_btc_arb([leg("kalshi", "UP", "0.48"), leg("polymarket", "DOWN", "0.49")],
                        window_match_secs=5, trusted=True)

    assert persist_btc_arbs(store, poll, arbs) == 1
    with store.connect() as con:
        [row] = list(con.execute("SELECT trusted, quality_flag FROM flat_bets WHERE kind='ARB_BTC'"))

    assert row["trusted"] == 1
    assert row["quality_flag"] is None


# ---- B1: single-venue intramarket YES+NO lock (the only fillable BTC edge) ----

def test_b1_intramarket_yes_no_lock_is_ok_and_same_venue():
    from arb.btc_arb import find_btc_intramarket_arb
    # Same Kalshi market: UP(yes)=0.48 + DOWN(no)=0.49 = 0.97 < 1 → guaranteed $1, status OK.
    arbs = find_btc_intramarket_arb([leg("kalshi", "UP", "0.48"), leg("kalshi", "DOWN", "0.49")])
    assert len(arbs) == 1
    assert arbs[0].status == "OK" and arbs[0].same_oracle is True
    assert arbs[0].up_leg.venue == "kalshi" and arbs[0].down_leg.venue == "kalshi"
    assert arbs[0].total_cost == Decimal("0.970000")


def test_b1_intramarket_requires_sum_below_one():
    from arb.btc_arb import find_btc_intramarket_arb
    assert find_btc_intramarket_arb([leg("kalshi", "UP", "0.52"), leg("kalshi", "DOWN", "0.49")]) == []


def test_b1_intramarket_does_not_pair_across_venues():
    from arb.btc_arb import find_btc_intramarket_arb
    # cross-venue UP/DOWN is the REVIEW lane's job, not the fillable intramarket one.
    assert find_btc_intramarket_arb([leg("kalshi", "UP", "0.48"), leg("polymarket", "DOWN", "0.49")]) == []


# ---- B2: Kalshi BTC legs priced on the quadratic fee curve, not linear ----

def test_b2_kalshi_btc_legs_use_quadratic_fee_basis():
    from decimal import Decimal as D

    from arb.experimental.btc_sweep import parse_kalshi_btc
    from arb.pricing import all_in_fee_cost, q6
    payload = {"markets": [{
        "ticker": "KXBTCD-26JUN2111-T72000", "yes_sub_title": "$72,000",
        "yes_ask_dollars": "0.50", "no_ask_dollars": "0.50",
        "close_time": "2026-06-21T11:00:00Z",
    }]}
    rate, buf = D("0.07"), D("0")
    legs = parse_kalshi_btc(payload, rate, buf)
    assert len(legs) == 2
    price = D("0.50")
    quad = q6(price + all_in_fee_cost(price, rate, buf, basis="quadratic"))
    lin = q6(price + all_in_fee_cost(price, rate, buf, basis="linear"))
    assert quad != lin  # the curves diverge at p=0.5 → this test actually distinguishes them
    assert all(l.effective_cost == quad for l in legs)


# ---- B4: windows matched on the true instant within tolerance, not a minute string ----

def test_b4_representational_minute_gap_still_matches_within_tolerance():
    from arb.btc_arb import find_btc_arb
    # 10:59:59 (Kalshi) vs 11:00:01 (Poly): 2s apart, but the OLD minute-string key (1059 vs
    # 1100) would have missed it. Same strike → same asset → matched within window_match_secs.
    a = leg("kalshi", "UP", "0.48", end=datetime(2026, 6, 21, 10, 59, 59, tzinfo=timezone.utc))
    b = leg("polymarket", "DOWN", "0.49", end=datetime(2026, 6, 21, 11, 0, 1, tzinfo=timezone.utc))
    arbs = find_btc_arb([a, b], window_match_secs=5, trusted=False)
    assert len(arbs) == 1 and arbs[0].total_cost == Decimal("0.970000")


def test_b4_distinct_windows_beyond_tolerance_do_not_match():
    from arb.btc_arb import find_btc_arb
    a = leg("kalshi", "UP", "0.48", end=datetime(2026, 6, 21, 11, 0, 0, tzinfo=timezone.utc))
    b = leg("polymarket", "DOWN", "0.49", end=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc))
    assert find_btc_arb([a, b], window_match_secs=5, trusted=False) == []


# ---- B1 ladder extension: single-venue lock from an inconsistent threshold ladder ----

def test_b1_ladder_inconsistency_is_a_fillable_lock():
    from arb.btc_arb import find_btc_ladder_arb
    # P(BTC>=72000) priced BELOW P(BTC>=73000) is impossible: UP@72000(0.40)+DOWN@73000(0.45)=0.85<1.
    # Buy UP at the lower strike + DOWN at the higher strike → pays >=$1 always → guaranteed lock.
    up_low = leg("kalshi", "UP", "0.40", asset="BTC@72000")
    down_high = leg("kalshi", "DOWN", "0.45", asset="BTC@73000")
    arbs = find_btc_ladder_arb([up_low, down_high])
    assert len(arbs) == 1
    assert arbs[0].status == "OK" and arbs[0].same_oracle is True
    assert arbs[0].total_cost == Decimal("0.850000")
    assert "ladder" in arbs[0].asset


def test_b1_ladder_consistent_ladder_has_no_arb():
    from arb.btc_arb import find_btc_ladder_arb
    # A monotone ladder: UP@72000(0.60) + DOWN@73000(0.50) = 1.10 >= 1 → no lock.
    up_low = leg("kalshi", "UP", "0.60", asset="BTC@72000")
    down_high = leg("kalshi", "DOWN", "0.50", asset="BTC@73000")
    assert find_btc_ladder_arb([up_low, down_high]) == []


def test_b1_ladder_requires_lower_up_strike_than_down_strike():
    from arb.btc_arb import find_btc_ladder_arb
    # UP at the HIGHER strike + DOWN at the LOWER strike is NOT a dominating lock — never paired.
    up_high = leg("kalshi", "UP", "0.40", asset="BTC@73000")
    down_low = leg("kalshi", "DOWN", "0.45", asset="BTC@72000")
    assert find_btc_ladder_arb([up_high, down_low]) == []


def test_b1_ladder_does_not_cross_venues_or_symbols():
    from arb.btc_arb import find_btc_ladder_arb
    # different venues (cross-venue is the REVIEW lane) and different underlyings never form a ladder.
    assert find_btc_ladder_arb([leg("kalshi", "UP", "0.40", asset="BTC@72000"),
                                leg("polymarket", "DOWN", "0.45", asset="BTC@73000")]) == []
    assert find_btc_ladder_arb([leg("kalshi", "UP", "0.40", asset="BTC@72000"),
                                leg("kalshi", "DOWN", "0.45", asset="ETH@73000")]) == []
