"""MODEL_FOUNDATIONS Phase B — settlement matrix enforcement (fixtures-free)."""
from decimal import Decimal

from arb.arbitrage import status_for_legs
from arb.models import Quote
from arb.settlement_matrix import load_matrix, section_green, section_issues

M = {
    "baseball_mlb": {
        "extra_innings": {"kalshi": "SETTLES", "polymarket": "SETTLES"},
        "suspended_game": {"kalshi": "UNKNOWN", "polymarket": "SETTLES"},  # a hole
    },
    "soccer": {
        "extra_time_scope": {"kalshi": "SETTLES", "polymarket": "SETTLES"},
    },
    "mma": {
        "no_contest": {"kalshi": "VOIDS", "polymarket": "VOIDS"},
        "cancelled_bout": {"kalshi": "CONDITIONAL: fair price", "polymarket": "VOIDS"},  # disagreement
    },
}


def test_section_green_requires_no_hole_and_no_disagreement():
    assert section_green("soccer", ["kalshi", "polymarket"], M)                 # all filled + agree
    assert not section_green("baseball_mlb", ["kalshi", "polymarket"], M)       # suspended_game UNKNOWN
    assert section_issues("baseball_mlb", ["kalshi", "polymarket"], M) == ["suspended_game"]
    assert not section_green("mma", ["kalshi", "polymarket"], M)                # cancelled_bout disagrees
    assert "cancelled_bout" in section_issues("mma", ["kalshi", "polymarket"], M)


def test_missing_section_is_not_green():
    assert not section_green("golf", ["kalshi", "polymarket"], {})


def test_real_matrix_mma_cancellation_blocks_trust():
    # The shipped matrix records the real Kalshi/Polymarket cancellation disagreement.
    assert not section_green("mma", ["kalshi", "polymarket"], load_matrix())


def _q(venue, sport):
    return Quote(event_key=f"{sport}:2026-07-11:a:b", venue=venue, book="b", market_type="WINNER",
                 line=None, outcome_label="a", raw_price=Decimal("0.5"), cost=Decimal("0.5"),
                 fee_cost=Decimal("0"), depth_usd=Decimal("100"), live=False,
                 fetched_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))


def test_trusted_pair_demoted_to_review_when_section_not_green():
    # Whitelisted mma pair, but the real matrix's cancelled_bout disagreement forces REVIEW.
    legs = [_q("kalshi", "mma"), _q("polymarket", "mma")]
    trusted = {"kalshi+polymarket:mma", "kalshi+polymarket"}
    status, notes = status_for_legs(legs, trusted)
    assert status == "REVIEW"
    assert any(n.startswith("settle-mismatch:mma-") for n in notes)
