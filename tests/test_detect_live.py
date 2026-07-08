"""L2: detect(mode='live') — inverted in-play guard, tighter skew, live tag,
status floored at REVIEW. Pregame behavior stays byte-identical (existing
test_arbitrage.py passes untouched)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.arbitrage import detect
from arb.config import load

from tests.test_arbitrage import NOW, group, q

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")
STARTED = NOW - timedelta(minutes=30)


def live_q(venue, outcome, cost, fetched=NOW, **kw):
    quote = q(venue, outcome, cost=cost, start=STARTED, fetched=fetched, **kw)
    quote.live = True
    return quote


def test_live_mode_flags_inplay_xspread_review_with_live_tag():
    g = group([live_q("kalshi", "mets", "0.47"), live_q("polymarket", "royals", "0.49")],
              start=STARTED)
    opps, _ = detect(CFG, [g], now=NOW, mode="live")
    locked = [o for o in opps if o.kind == "XSPREAD"]
    assert len(locked) == 1
    o = locked[0]
    assert o.status == "REVIEW"      # floors at REVIEW in live mode
    assert o.live is True            # carried into the candidate dict downstream
    assert o.edge == Decimal("0.04")


def test_live_mode_rejects_pregame_groups_as_diagnostic():
    g = group([q("kalshi", "mets", cost="0.47"), q("kalshi", "royals", cost="0.51")])
    opps, rejects = detect(CFG, [g], now=NOW, mode="live")
    assert not opps
    assert any(r["reason"] == "pregame-in-live-cycle" for r in rejects)


def test_live_mode_same_venue_arb_still_review_floored():
    # same-venue in-play ARB: taxonomy unchanged, but live mode floors REVIEW
    g = group([live_q("kalshi", "mets", "0.47"), live_q("kalshi", "royals", "0.51")],
              start=STARTED)
    opps, _ = detect(CFG, [g], now=NOW, mode="live")
    arb = [o for o in opps if o.kind == "ARB"]
    assert arb and arb[0].status == "REVIEW" and arb[0].live


def test_live_skew_bound_is_tighter():
    # 5s apart: fine pregame (10s bound), REVIEW in live mode (3s bound)
    g = group([live_q("kalshi", "mets", "0.47"),
               live_q("kalshi", "royals", "0.51", fetched=NOW - timedelta(seconds=5))],
              start=STARTED)
    opps, _ = detect(CFG, [g], now=NOW, mode="live")
    arb = [o for o in opps if o.kind == "ARB"][0]
    assert "skew" in arb.notes


def test_live_mode_guards_still_apply():
    # zombie guard does not relax in-play
    g = group([live_q("kalshi", "mets", "0.95", vol="0", bid="0.01"),
               live_q("kalshi", "royals", "0.02")], start=STARTED)
    opps, rejects = detect(CFG, [g], now=NOW, mode="live")
    assert not [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert any(r["reason"] == "zombie" for r in rejects)


def test_live_mode_soccer_covering_set_never_relaxes():
    # a soccer game in the 80th minute still has three outcomes
    g = group([live_q("kalshi", "arsenal", "0.45", sport="soccer"),
               live_q("polymarket", "chelsea", "0.45", sport="soccer")],
              sport="soccer", start=STARTED)
    opps, rejects = detect(CFG, [g], now=NOW, mode="live")
    assert not [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert any(r["reason"] == "covering-set-fail" for r in rejects)
