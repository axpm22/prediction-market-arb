"""NegRisk basket scan + exhaustiveness proof (SPEC §8.4 conditions a–d, §7.2).

Structural note pinned by these tests: with the A1 defaults (min_edge 0.006,
mids_sum_min 0.995) a YES basket can never be clean — ask >= mid forces
mids_sum <= asks_sum = 1 - edge <= 0.994 < 0.995, i.e. any YES basket with a
bookable edge fails the window. The NO side (books overpriced, mids_sum > 1)
is the only construct that can clear cleanly — consistent with the addendum's
NO-basket lockup discussion."""
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.negrisk import exhaustiveness_proof, scan_group

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")


def out(label, yes_ask, yes_bid, no_ask=None, no_bid=None, depth="50"):
    return {"label": label, "yes_ask": Decimal(yes_ask), "yes_bid": Decimal(yes_bid),
            "no_ask": Decimal(no_ask) if no_ask else None,
            "no_bid": Decimal(no_bid) if no_bid else None,
            "depth_usd": Decimal(depth), "volume_24h": Decimal(10)}


def three(spread="0.0015"):
    s = Decimal(spread)
    outs = []
    for i, mid in enumerate((Decimal("0.34"), Decimal("0.33"), Decimal("0.33"))):
        outs.append(out(f"o{i}", str(mid + s), str(mid - s),
                        no_ask=str(1 - mid + s), no_bid=str(1 - mid - s)))
    return outs


def test_proof_passes_tight_group():
    status, reason = exhaustiveness_proof(CFG, three(), venue_flag=True)
    assert status == "proven", reason


def test_proof_fails_without_venue_flag():
    status, reason = exhaustiveness_proof(CFG, three(), venue_flag=False)
    assert status == "unproven-exhaustive" and "venue-flag" in reason


def test_proof_fails_mids_below_window():
    # §7.2 exhaustiveness vector: mids summing 0.93 = ~7% unlisted tail
    outs = [out("a", "0.315", "0.305"), out("b", "0.315", "0.305"),
            out("c", "0.315", "0.305")]
    status, reason = exhaustiveness_proof(CFG, outs, venue_flag=True)
    assert status == "unproven-exhaustive" and "mids-sum" in reason


def test_proof_fails_one_sided_outcome():
    outs = three()
    outs[2]["yes_bid"] = None  # unquoted side silently weakens the proof (condition c)
    status, reason = exhaustiveness_proof(CFG, outs, venue_flag=True)
    assert status == "unproven-exhaustive" and "two-sided" in reason


def test_proof_fails_wide_spreads():
    # summed half-spreads above the window slack mask the tail (condition d)
    outs = three(spread="0.02")
    status, reason = exhaustiveness_proof(CFG, outs, venue_flag=True)
    assert status == "unproven-exhaustive" and "half-spread" in reason


def test_yes_basket_with_bookable_edge_is_structurally_unproven():
    # asks sum 0.9925 (edge 0.0075) forces mids below the window -> REVIEW, never clean
    outs = [out("o0", "0.3325", "0.3305", no_ask="0.669", no_bid="0.667"),
            out("o1", "0.33", "0.328", no_ask="0.672", no_bid="0.670"),
            out("o2", "0.33", "0.328", no_ask="0.672", no_bid="0.670")]
    opps = scan_group(CFG, "polymarket", "geopolitics", "ev1", "t", outs, venue_flag=True)
    yes = [o for o in opps if o["side"] == "YES"]
    assert len(yes) == 1
    assert yes[0]["edge_points"] == Decimal("0.0075")
    assert yes[0]["proof_status"] == "unproven-exhaustive"
    assert yes[0]["status"] == "REVIEW"


def test_no_basket_can_be_clean():
    # yes mids 0.339 x3 (sum 1.017, in window); NO asks 0.6625 x3 -> S=1.9875 < 2
    outs = [out(f"o{i}", "0.3405", "0.3375", no_ask="0.6625", no_bid="0.6595")
            for i in range(3)]
    opps = scan_group(CFG, "polymarket", "geopolitics", "ev1", "t", outs, venue_flag=True)
    no = [o for o in opps if o["side"] == "NO"]
    assert len(no) == 1
    assert no[0]["proof_status"] == "proven"
    assert no[0]["edge_points"] == Decimal("0.00625")   # 1 - 1.9875/2
    assert no[0]["status"] == "OK"


def test_no_basket_vector_and_break_even():
    # §7.2: NO asks (0.65,0.66,0.67) sum 1.98 < 2 -> flags with edge 0.01
    outs = [out("a", "0.345", "0.335", no_ask="0.65", no_bid="0.64"),
            out("b", "0.335", "0.325", no_ask="0.66", no_bid="0.65"),
            out("c", "0.325", "0.315", no_ask="0.67", no_bid="0.66")]
    opps = scan_group(CFG, "polymarket", "geopolitics", "ev1", "t", outs, venue_flag=True)
    no = [o for o in opps if o["side"] == "NO"]
    assert len(no) == 1 and no[0]["edge_points"] == Decimal("0.01")
    # (0.67 x3) = 2.01 must NOT flag
    flat = [out("a", "0.345", "0.335", no_ask="0.67", no_bid="0.66"),
            out("b", "0.335", "0.325", no_ask="0.67", no_bid="0.66"),
            out("c", "0.325", "0.315", no_ask="0.67", no_bid="0.66")]
    opps = scan_group(CFG, "polymarket", "geopolitics", "ev1", "t", flat, venue_flag=True)
    assert not [o for o in opps if o["side"] == "NO"]


def test_unproven_group_records_as_review_never_dropped():
    outs = [out(f"o{i}", "0.3405", "0.3375", no_ask="0.6625", no_bid="0.6595")
            for i in range(3)]
    opps = scan_group(CFG, "polymarket", "geopolitics", "ev1", "t", outs, venue_flag=False)
    no = [o for o in opps if o["side"] == "NO"]
    assert no and no[0]["status"] == "REVIEW"
    assert no[0]["proof_status"] == "unproven-exhaustive"
