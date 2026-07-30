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


def test_xspread_demoted_pair_never_clean(monkeypatch):
    # rule 12: trust is part of the ONE predicate. classify used to check
    # trusted_pairs only, so a mismatch-demoted pair still classified clean
    # while booking_trusted stamped trusted=0 — lane and trust flag disagreed.
    #
    # The settlement matrix ships every cell AMBER, so the control below ("a
    # certified pair IS clean") cannot hold on a real matrix once classify
    # consults it. Force GREEN so this test keeps testing what it was written
    # for — that pair_demoted blocks a pair that would OTHERWISE pass — rather
    # than passing for the unrelated reason that the matrix is unverified.
    import arb.settlement_matrix as sm
    monkeypatch.setattr(sm, "sport_all_green", lambda matrix, sport: True)
    cfg = dict(CFG)
    cfg["trusted_pairs"] = ["kalshi:polymarket"]
    cfg = {**cfg, "deployment": {**cfg["deployment"], "xspread_autobook": True}}
    base = {"kind": "XSPREAD", "category": "sports", "sport": "baseball_mlb",
            "venue_pair": "kalshi:polymarket", "status": "OK",
            "quality_flag": "none", "live": False, "start_grade": "verified",
            "pregame": True, "leg_skew_secs": 0.0, "depth_ok": True,
            "edge_points": "0.05", "roi": "0.03", "certificate_n": 700,
            "micro": False, "ai_matched": False}
    ok, _, lane = classify(base, cfg)
    assert (ok, lane) == (True, "clean")  # control: certified pair is clean
    ok, reason, lane = classify({**base, "pair_demoted": True}, cfg)
    assert (ok, lane) == (False, "review")
    assert reason == "pair-demoted"


def _xspread_permissive_cfg():
    """A config that clears every gate BEFORE the matrix check, so the matrix is
    what the assertion is actually testing. Not a production config."""
    c = dict(CFG)
    c["trusted_pairs"] = ["kalshi:polymarket"]
    c["deployment"] = {**CFG["deployment"], "xspread_autobook": True}
    return c


def test_xspread_requires_a_green_settlement_matrix():
    """eligibility gated trust on trusted_pairs and never consulted the matrix;
    sport_all_green had ZERO callers, so the settlement-rule verification begun
    2026-07-29 had no path to affecting behaviour — the §18.2 gate was
    disconnected, not closed. tennis.walkover is RED on primary-source evidence
    (Kalshi resolves a pre-match cancellation to 'a fair price in accordance with
    the rules'; Polymarket to a hard 50-50)."""
    eligible, reason, lane = classify(
        obj(kind="XSPREAD", venue_pair="kalshi:polymarket", sport="tennis",
            certificate_n=1000, edge_points=Decimal("0.50")),
        _xspread_permissive_cfg())
    assert not eligible
    assert reason == "matrix-not-green"
    assert lane == "review"


def test_matrix_gate_does_not_touch_same_venue_arb():
    """The matrix is about two venues agreeing. A same-venue ARB has one oracle
    and must be unaffected."""
    eligible, reason, lane = classify(obj(sport="tennis"), _xspread_permissive_cfg())
    assert (eligible, lane) == (True, "clean"), f"got {reason}/{lane}"


def test_sx_never_reaches_the_clean_lane():
    """0 of 356,595 stored SX quotes carry a bid, so an SX leg can never be
    unwound or marked; reconcile._sx returns None unconditionally (settled-status
    semantics unverified) so SX legs never grade and no certificate can accrue;
    and the chain is being deprecated. Yet BOTH clean-lane positions in the whole
    ledger were SX, and SX legs quoted at 0.001-0.041 produced every top-edge
    artifact in the ledger at 15-98% 'edges'."""
    eligible, reason, lane = classify(obj(venue_pair="sx:self"), CFG)
    assert not eligible
    assert reason == "venue-excluded"
    assert lane == "review"


def test_an_excluded_venue_on_either_leg_blocks_the_pair():
    eligible, reason, _ = classify(
        obj(kind="XSPREAD", venue_pair="polymarket:sx"), CFG)
    assert not eligible and reason == "venue-excluded"


def test_a_non_excluded_same_venue_arb_still_reaches_clean():
    """The guard against Task 5 emptying the clean lane by accident rather than
    by rule: a kalshi self-pair must still promote."""
    eligible, reason, lane = classify(obj(venue_pair="kalshi:self"), CFG)
    assert (eligible, lane) == (True, "clean"), f"got {reason}/{lane}"
