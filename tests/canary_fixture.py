"""Deterministic canary fixture (SPEC §13.3): event, quotes, resolution all
predetermined; expected P&L pinned (ARB $0.041667, EV $0.932367). Runs through
the REAL tracker/settle/metrics pipeline in the tagged canary lane (category
'canary' -> review lane, never headline)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from arb.models import Event, Opportunity, Quote


def _q(event_key, venue, outcome, cost, fee, start, now):
    c = Decimal(cost)
    return Quote(event_key=event_key, venue=venue, book=venue, market_type="WINNER",
                 line=None, outcome_label=outcome, side="YES", raw_price=c, cost=c,
                 fee_cost=Decimal(fee), depth_usd=Decimal("100"), ladder_json=None,
                 live=False, fetched_at=now, category="canary", sport="canary",
                 bid=c - Decimal("0.02"), volume_24h=Decimal(1), home="canary-yes",
                 away="canary-no", start_utc=start, start_grade="nominal",
                 venue_side="YES")


def canary_opps(tag: str, now):
    event_key = f"canary:{tag}"
    start = now + timedelta(hours=2)
    ev = Event(event_key=event_key, category="canary", sport="canary",
               home="canary-yes", away="canary-no", start_utc=start,
               start_grade="nominal")
    arb = Opportunity(
        kind="ARB", category="canary", event=ev,
        legs=[_q(event_key, "kalshi", "canary-yes", "0.47", "0", start, now),
              _q(event_key, "kalshi", "canary-no", "0.49", "0", start, now)],
        edge=Decimal("0.04"), stakes=[], status="OK", notes="canary")
    ev_leg = _q(event_key, "kalshi", "canary-yes", "0.50", "0.0175", start, now)
    evo = Opportunity(kind="EV", category="canary", event=ev, legs=[ev_leg],
                      edge=Decimal("0.0325"), stakes=[], status="OK",
                      notes="canary p_hat=0.55")
    return [arb, evo], event_key
