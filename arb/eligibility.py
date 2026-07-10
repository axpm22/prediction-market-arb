"""THE eligibility predicate (SPEC §12, rule 12). The ONLY module that decides
(eligible, reason, lane). Detection, tracker, settle, reconcile, capacity,
exposure, and report import classify(); none reimplements any part of it.
A grep-test (test_hygiene) proves `def classify(` exists only here.

Input: a flat dict of pinned keys (both Opportunity-shaped and bet-row-shaped
callers build it): kind, category, sport, venue_pair, status, quality_flag,
live, start_grade, pregame, leg_skew_secs, depth_ok, edge_points, roi,
proof_status, ai_matched, micro, certificate_n (optional), pair_demoted
(optional — the caller pins the pair_demotions DB fact).
"""
from __future__ import annotations

from decimal import Decimal

from arb.models import LOCKED_KINDS


def _velocity(cfg, sport: str) -> Decimal:
    table = cfg["execution"]["line_velocity_points_per_sec"]
    return Decimal(table.get(sport, table["default"]))


def classify(o: dict, cfg) -> tuple[bool, str, str]:
    """Returns (eligible, reason, lane) with lane in {'clean','review','invalid'}."""
    kind = o["kind"]

    # category segregation (rule 11, §10.6): non-sports rows live in their own
    # report section (the headline filter is trusted+sports+pregame+clean), but
    # only single-venue locked kinds may be clean there (§12); everything else
    # non-sports is review.
    is_sports = o.get("category") == "sports"
    if o.get("ai_matched"):
        return False, "ai-matched", "review"
    if not is_sports and kind not in ("NEGRISK", "POLITICAL"):
        return False, "nonsports-segregated", "review"
    if o.get("live"):
        return False, "live", "review"

    # timing (§8.4 guards, restated for every money-touching caller);
    # start/pregame are sports concepts — non-sports gates on resolution horizon
    # at booking (nonsports_max_days, tracker) instead
    if is_sports and o.get("start_grade") == "absent":
        return False, "start-unknown", "review"
    if is_sports and not o.get("pregame", True):
        return False, "in-play", "review"

    # quality quarantine (labels, never deletes)
    flag = o.get("quality_flag", "none")
    if flag and flag != "none":
        return False, flag.replace("_", "-"), "review"

    # depth floor (§8.4)
    if not o.get("depth_ok"):
        return False, "depth-floor", "review"

    # simultaneity + velocity decay (§8.4)
    skew = Decimal(str(o.get("leg_skew_secs") or 0))
    if skew > cfg["execution"]["max_actionable_skew_secs"]:
        return False, "skew", "review"
    adj = Decimal(str(o.get("edge_points") or 0)) - skew * _velocity(cfg, o.get("sport", ""))
    if kind != "DIVERGENCE" and adj <= cfg["execution"]["min_adj_edge_points"]:
        return False, "adj-edge", "review"

    # stale-quote ceiling for EVERY locked kind (§8.4, v3.3)
    roi = o.get("roi")
    if kind in LOCKED_KINDS and roi is not None and Decimal(str(roi)) > cfg["arb_stale_warn"]:
        return False, "stale-warn", "review"

    # detection-status REVIEW can never launder into clean (bug A1)
    if o.get("status") == "REVIEW":
        return False, o.get("reason") or "detection-review", "review"

    # kind-specific gates
    if kind == "XSPREAD":
        # rule of three: net = gross − Σfees − (3/n)·LGM; n=0 → ineligible outright (§12)
        n = int(o.get("certificate_n") or 0)
        if n == 0:
            return False, "no-certificate", "review"
        if not cfg["deployment"]["xspread_autobook"]:
            return False, "xspread-structural-disable", "review"
        net = (Decimal(str(o["edge_points"]))
               - Decimal(3) / Decimal(n) * cfg["mismatch_pricing"]["lgm"])
        if net <= 0:
            return False, "net-spread-nonpositive", "review"
        pair = o.get("venue_pair", "")
        if pair not in [str(p) for p in cfg["trusted_pairs"]]:
            return False, "untrusted-pair", "review"
        if o.get("pair_demoted"):
            # settlement-mismatch demotion (§11.2) — the caller pins the DB fact;
            # rule 12: lane and the booking_trusted stamp must never disagree
            return False, "pair-demoted", "review"
        return True, "xspread-certified", "clean"

    if kind == "NEGRISK":
        if o.get("proof_status") != "proven":
            return False, "unproven-exhaustive", "review"
        return True, "negrisk-proven", "clean"

    if kind == "EV":
        if not cfg["ev_enabled"]:
            return False, "ev-frozen", "review"
        if o.get("micro") and not cfg["micro"]["enabled"]:
            return False, "micro-disabled", "review"
        return True, "ev", "clean"

    if kind == "BTC":
        if not cfg["btc_arb"]["enabled"] or not cfg["btc_arb"]["trusted"]:
            return False, "btc-untrusted", "review"
        return True, "btc", "clean"

    if kind == "DIVERGENCE":
        return False, "divergence-never-a-bet", "review"

    if kind == "PARLAY":
        return False, "parlay-signal-only", "review"

    if kind in ("ARB", "POLITICAL"):
        # same-venue locked constructs: trusted by construction (§12), guards above
        pair = o.get("venue_pair", "")
        if ":" in pair and not pair.endswith(":self"):
            # a cross-venue object mislabeled ARB is a taxonomy violation (RULE 1)
            return False, "cross-venue-mislabeled-arb", "invalid"
        return True, "single-venue-locked", "clean"

    return False, f"unknown-kind-{kind}", "invalid"
