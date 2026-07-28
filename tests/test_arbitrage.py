"""Detection (SPEC §8.4): constructs, covering-set proof, guards, taxonomy."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.arbitrage import detect
from arb.config import load
from arb.matching import MatchedGroup
from arb.models import Event, Quote

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")
NOW = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=2)


def q(venue, outcome, side="YES", cost="0.50", sport="baseball_mlb", market_type="WINNER",
      line=None, start=START, depth="100", vol="10", fetched=NOW, bid=None):
    c = Decimal(cost)
    return Quote(event_key="e", venue=venue, book=venue, market_type=market_type,
                 line=line, outcome_label=outcome, side=side, raw_price=c, cost=c,
                 fee_cost=Decimal("0"), depth_usd=Decimal(depth) if depth else None,
                 ladder_json=None, live=False, fetched_at=fetched, category="sports",
                 sport=sport, bid=Decimal(bid) if bid else c - Decimal("0.02"),
                 volume_24h=Decimal(vol) if vol is not None else None,
                 home="h", away="a", start_utc=start, start_grade="verified")


def group(quotes, sport="baseball_mlb", suspect=None, start=START):
    ev = Event(event_key="e", category="sports", sport=sport, home="h", away="a",
               start_utc=start, start_grade="verified")
    return MatchedGroup(event=ev, quotes=quotes, suspect=suspect)


def detect_all(g):
    opps, rejects = detect(CFG, [g], now=NOW)
    return opps, rejects


def test_cross_venue_covering_pair_is_xspread_never_arb():
    g = group([q("kalshi", "mets", cost="0.47"), q("polymarket", "royals", cost="0.49")])
    opps, _ = detect_all(g)
    locked = [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert len(locked) == 1
    o = locked[0]
    assert o.kind == "XSPREAD"          # RULE 1: cross-venue is never called ARB
    assert o.status == "REVIEW"          # cross-venue REVIEW-only forever by default
    assert o.edge == Decimal("0.04")     # 1 - 0.96


def test_same_venue_covering_pair_is_arb():
    g = group([q("kalshi", "mets", cost="0.47"), q("kalshi", "royals", cost="0.51")])
    opps, _ = detect_all(g)
    locked = [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert locked and locked[0].kind == "ARB"
    assert locked[0].status == "OK"


def test_soccer_two_yes_legs_never_flag():
    # K-YES(A) 0.45 + P-YES(B) 0.45 = 0.90 but a draw loses both (§7.2 vector)
    g = group([q("kalshi", "arsenal", cost="0.45", sport="soccer"),
               q("polymarket", "chelsea", cost="0.45", sport="soccer")], sport="soccer")
    opps, rejects = detect_all(g)
    assert not [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert any(r["reason"] == "covering-set-fail" for r in rejects)


def test_soccer_same_proposition_yes_no_is_the_endorsed_construct():
    g = group([q("kalshi", "arsenal", side="YES", cost="0.45", sport="soccer"),
               q("polymarket", "arsenal", side="NO", cost="0.50", sport="soccer")],
              sport="soccer")
    opps, _ = detect_all(g)
    locked = [o for o in opps if o.kind == "XSPREAD"]
    assert len(locked) == 1 and locked[0].edge == Decimal("0.05")


def test_stale_warn_roi_books_review_not_celebration():
    # 4.17% locked return would trip arb_stale_warn (trap 1) — even same-venue
    g = group([q("kalshi", "mets", cost="0.47"), q("kalshi", "royals", cost="0.49")])
    opps, _ = detect_all(g)
    o = [x for x in opps if x.kind == "ARB"][0]
    assert o.status == "REVIEW" and "stale" in o.notes


def test_in_play_leg_never_pairs():
    g = group([q("kalshi", "mets", cost="0.47", start=NOW - timedelta(minutes=5)),
               q("kalshi", "royals", cost="0.51", start=NOW - timedelta(minutes=5))],
              start=NOW - timedelta(minutes=5))
    opps, rejects = detect_all(g)
    assert not opps
    assert any(r["reason"] == "in-play" for r in rejects)


def test_zombie_quote_excluded():
    # zero 24h volume AND (ask - bid) >= 0.90: a zombie book, not a market (§8.4)
    g = group([q("kalshi", "mets", cost="0.95", vol="0", bid="0.01"),
               q("kalshi", "royals", cost="0.02")])
    opps, rejects = detect_all(g)
    assert not [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert any(r["reason"] == "zombie" for r in rejects)


def test_skew_beyond_outer_bound_never_pairs():
    g = group([q("kalshi", "mets", cost="0.47"),
               q("polymarket", "royals", cost="0.49", fetched=NOW - timedelta(seconds=90))])
    opps, rejects = detect_all(g)
    assert not [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert any(r["reason"] == "skew-outer" for r in rejects)


def test_totals_pair_every_strike_exact_line_only():
    g = group([q("kalshi", "OVER", market_type="TOTAL", line="8.5", cost="0.48"),
               q("polymarket", "UNDER", market_type="TOTAL", line="8.5", cost="0.47"),
               q("polymarket", "UNDER", market_type="TOTAL", line="9.5", cost="0.30")])
    opps, _ = detect_all(g)
    locked = [o for o in opps if o.kind == "XSPREAD"]
    assert len(locked) == 1
    assert {leg.line for leg in locked[0].legs} == {"8.5"}  # 8.5 never pairs 9.5


def test_divergence_recorded_no_ledger_claim():
    g = group([q("kalshi", "mets", cost="0.50", bid="0.48"),
               q("polymarket", "mets", cost="0.58", bid="0.56")])
    opps, _ = detect_all(g)
    div = [o for o in opps if o.kind == "DIVERGENCE"]
    assert len(div) == 1
    assert div[0].edge == Decimal("0.08")  # raw-mid spread 0.57-0.49
    assert div[0].status == "REVIEW"


def test_match_suspect_group_never_arbs():
    g = group([q("kalshi", "mets", cost="0.30"), q("polymarket", "royals", cost="0.45")],
              suspect="match-suspect-price")
    opps, _ = detect_all(g)
    assert not [o for o in opps if o.kind in ("ARB", "XSPREAD")]


def test_match_suspect_group_logs_tagged_divergence():
    # §8.2: suspect groups may log divergences (tagged), never locked flags.
    # The old `continue` skipped the divergence scan entirely (dead ternary).
    g = group([q("kalshi", "mets", cost="0.50", bid="0.48"),
               q("polymarket", "mets", cost="0.58", bid="0.56")],
              suspect="match-suspect-price")
    opps, rejects = detect_all(g)
    assert not [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    div = [o for o in opps if o.kind == "DIVERGENCE"]
    assert len(div) == 1
    assert div[0].notes == "match-suspect"
    assert div[0].status == "REVIEW"
    # the suspect reject stays on the audit trail
    assert any(r["reason"] == "match-suspect-price" for r in rejects)


def test_far_from_start_demotes_to_review():
    """Measured 2026-07-28: legs flagged >48h before first pitch sit on books
    with a median 24h volume of 3 (vs 211,365 within 6h), and half have zero
    volume outright. Those are early lines nobody is trading, and they were the
    single largest source of flags in the ledger — 89 of 125. The zombie guard
    misses them because it also demands a >=90c spread, which catches only 0.8%
    of zero-volume quotes. Demote, never drop: rule 7 keeps the row and REVIEW
    keeps it out of the headline."""
    far = NOW + timedelta(hours=70)
    g = group([q("kalshi", "mets", cost="0.47", start=far),
               q("polymarket", "royals", cost="0.49", start=far)], start=far)
    opps, _ = detect(CFG, [g], now=NOW)
    locked = [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert locked, "a covering pair should still be flagged"
    assert locked[0].status == "REVIEW"
    assert "lead" in locked[0].notes


def test_near_start_is_not_demoted_for_lead():
    g = group([q("kalshi", "mets", cost="0.47"), q("polymarket", "royals", cost="0.49")])
    opps, _ = detect(CFG, [g], now=NOW)
    locked = [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert locked
    assert "lead" not in locked[0].notes
