"""T-ELIG: the single predicate (SPEC §12). A REVIEW/cross-venue/unproven object
can NEVER reach the clean lane; the frozen fixture tests pin it."""
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.eligibility import classify

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")


def obj(**kw):
    base = dict(kind="ARB", category="sports", sport="baseball_mlb", venue_pair="kalshi:self",
                status="OK", quality_flag="none", live=False, start_grade="verified",
                pregame=True, leg_skew_secs=2.0, depth_ok=True, edge_points=Decimal("0.01"),
                roi=Decimal("0.012"), proof_status="proven", ai_matched=False, micro=False)
    base.update(kw)
    return base


def test_clean_single_venue_arb_is_eligible():
    eligible, reason, lane = classify(obj(), CFG)
    assert (eligible, lane) == (True, "clean")


def test_review_status_never_clean():
    _, reason, lane = classify(obj(status="REVIEW"), CFG)
    assert lane == "review"


def test_xspread_never_clean_without_certificate():
    # n=0 (no certificate) is ineligible outright, no division performed (§12)
    eligible, reason, lane = classify(obj(kind="XSPREAD", venue_pair="kalshi:polymarket"),
                                      CFG)
    assert lane == "review" and not eligible
    assert "certificate" in reason or "xspread" in reason


def test_xspread_structurally_disabled_even_with_certificate():
    # deployment.xspread_autobook: false — the auto-book window is the empty set
    eligible, _, lane = classify(obj(kind="XSPREAD", venue_pair="kalshi:polymarket",
                                     certificate_n=1000), CFG)
    assert lane == "review" and not eligible


def test_cross_venue_pair_not_whitelisted_is_review():
    _, reason, lane = classify(obj(venue_pair="kalshi:polymarket", kind="XSPREAD"), CFG)
    assert lane == "review"


def test_unproven_exhaustive_basket_never_clean():
    _, reason, lane = classify(obj(kind="NEGRISK", proof_status="unproven-exhaustive"), CFG)
    assert lane == "review" and reason == "unproven-exhaustive"


def test_in_play_review():
    assert classify(obj(pregame=False), CFG)[2] == "review"
    assert classify(obj(live=True), CFG)[2] == "review"


def test_start_unknown_never_actionable():
    assert classify(obj(start_grade="absent"), CFG)[2] == "review"


def test_quality_flag_quarantines():
    assert classify(obj(quality_flag="zombie"), CFG)[2] == "review"


def test_depth_floor():
    assert classify(obj(depth_ok=False), CFG)[2] == "review"


def test_skew_gate_with_velocity_decay():
    # 30s skew at mlb velocity 0.0008/s decays a 1pt edge to negative -> review
    assert classify(obj(leg_skew_secs=30.0, edge_points=Decimal("0.01")), CFG)[2] == "review"
    # 2s skew barely dents it -> clean
    assert classify(obj(leg_skew_secs=2.0), CFG)[2] == "clean"


def test_stale_warn_books_review_for_every_locked_kind():
    for kind in ("ARB", "NEGRISK", "POLITICAL", "BTC"):
        lane = classify(obj(kind=kind, roi=Decimal("0.05"),
                            proof_status="proven"), CFG)[2]
        assert lane == "review", kind


def test_ev_frozen():
    assert classify(obj(kind="EV", venue_pair="kalshi:self"), CFG)[2] == "review"


def test_ai_matched_always_review():
    assert classify(obj(ai_matched=True), CFG)[2] == "review"


def test_nonsports_segregated():
    # non-sports: only single-venue locked kinds may be clean (own report section,
    # never headline — the headline filter is trusted+sports+pregame+clean)
    assert classify(obj(category="nonsports", kind="EV"), CFG)[2] == "review"
    assert classify(obj(category="nonsports", kind="XSPREAD",
                        venue_pair="kalshi:polymarket"), CFG)[2] == "review"
    ok, _, lane = classify(obj(category="nonsports", kind="NEGRISK",
                               venue_pair="polymarket:self", start_grade="absent"), CFG)
    assert (ok, lane) == (True, "clean")
