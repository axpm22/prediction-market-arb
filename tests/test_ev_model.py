from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.cli import detect_ev_from_consensus
from arb.config import load_config
from arb.models import Quote
from arb.pricing import clv_points, edge_points, kelly_fraction, q6
from arb.store import Store
from arb.experimental.ev.clv import grade_clv, venue_close

EVENT = "baseball_mlb:2026-06-12:kansas city royals:texas rangers:3"


def quote(cost: str, depth: str = "500") -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=EVENT, venue="kalshi", book="KXMLB-KC", market_type="WINNER",
        line=None, outcome_label="kansas city royals", raw_price=price, cost=price,
        fee_cost=Decimal("0"), depth_usd=Decimal(depth), live=False,
        fetched_at=datetime.now(timezone.utc), start_utc=datetime.now(timezone.utc) + timedelta(hours=4),
    )


# ---- §3 / §7 pure math (spec vectors) ----

def test_edge_points_and_kelly_spec_example() -> None:
    # EV_SPEC §8: p=0.55, a=0.49 → edge 6 points, full Kelly (p-a)/(1-a).
    p, a = Decimal("0.55"), Decimal("0.49")
    assert edge_points(p, a) == Decimal("0.060000")
    full = (p - a) / (Decimal("1") - a)
    assert kelly_fraction(p, a, Decimal("0.25")) == q6(Decimal("0.25") * full)


def test_kelly_zero_without_edge() -> None:
    assert kelly_fraction(Decimal("0.45"), Decimal("0.50"), Decimal("0.25")) == Decimal("0")


def test_clv_points_sign() -> None:
    # bought at 0.49, venue closed at 0.54 → +5 points captured (no game needed).
    assert clv_points(Decimal("0.54"), Decimal("0.49")) == Decimal("0.050000")


# ---- detection: floor, review, kelly/capturable notes ----

def test_ev_flag_below_emin_recorded_but_tagged() -> None:
    config = load_config()
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.52"), datetime.now(timezone.utc))}
    [opp] = detect_ev_from_consensus([quote("0.50")], consensus, 8, set(),
                                     e_min=Decimal("0.05"), config=config)
    # 0.52 - 0.50 = 2 points < 5pt floor: recorded, tagged below-e-min, and EV's killer.
    assert "below-e-min" in opp.notes
    assert "event-match-review" in opp.notes
    assert opp.status == "REVIEW"
    assert opp.kelly_f_star is not None      # Phase 3 projection attached
    assert opp.quality_flag is None          # 0.50 ask, $500 depth, 2pt edge → clean
    assert any(n.startswith("capturable:") for n in opp.notes)


def test_ev_flag_clearing_floor_is_not_below_emin() -> None:
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.56"), datetime.now(timezone.utc))}
    [opp] = detect_ev_from_consensus([quote("0.50")], consensus, 8, set(), e_min=Decimal("0.05"))
    assert "below-e-min" not in opp.notes        # 6 points clears the floor
    assert "event-match-review" in opp.notes      # but still REVIEW until whitelisted


# ---- §5 CLV grader over the archive ----

def test_clv_grader_uses_depth_floored_venue_close(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("watch")
    # the venue's last pregame quote on the same book = the close (0.54, deep)
    store.save_poll_quotes(poll, [quote("0.54", depth="500")])
    close = venue_close(store, EVENT, "kalshi", "KXMLB-KC", "kansas city royals", Decimal("50"))
    assert close == Decimal("0.54")
    # a thin close is rejected (would fake signal)
    store.save_poll_quotes(store.start_poll("watch"), [quote("0.54", depth="10")])
    assert venue_close(store, EVENT, "kalshi", "KXMLB-KC", "kansas city royals", Decimal("50")) is None
