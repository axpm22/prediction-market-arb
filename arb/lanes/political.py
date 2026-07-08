"""POLITICAL lane (SPEC §12): intra-market YES+NO < 1, POLYMARKET ONLY — the
YES and NO books are independent CLOB instruments there. Kalshi is NEVER
scanned: `no_ask = 1 - yes_bid` by construction, so an intra-market "hit" is a
crossed-book feed artifact, not an arb (§6.1). Locked at entry; exempt from
nonsports_max_days (political markets run months)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from arb.models import Event, Opportunity, Quote, q6
from arb.venues.fees import leg_fee_cost


def scan_market(cfg, market: dict, books: dict, category: str = "politics"
                ) -> Opportunity | None:
    """One Polymarket binary market: YES ask + NO ask (each + fee) < 1 -> locked."""
    tokens = json.loads(market.get("clobTokenIds") or "[]")
    if len(tokens) != 2:
        return None
    legs = []
    now = datetime.now(timezone.utc)
    for token, side in zip(tokens, ("YES", "NO")):
        book = books.get(token)
        if not book or not book.get("asks"):
            return None
        ask = min(Decimal(a["price"]) for a in book["asks"])
        bids = book.get("bids") or []
        size = [a for a in book["asks"] if Decimal(a["price"]) == ask][0]["size"]
        legs.append(Quote(
            event_key=f"nonsports:pm:{market.get('conditionId', market.get('id'))}",
            venue="polymarket", book="polymarket", market_type="WINNER", line=None,
            outcome_label=market.get("question", "")[:80], side=side,
            venue_side=side, raw_price=ask, cost=ask,
            fee_cost=leg_fee_cost(cfg, "polymarket", category, ask),
            depth_usd=Decimal(size) * ask, ladder_json=None, live=False,
            fetched_at=now, category="nonsports", sport="politics",
            bid=max((Decimal(b["price"]) for b in bids), default=None),
            volume_24h=Decimal(str(market.get("volume24hr") or 0)),
            venue_market_id=f"{market.get('conditionId', market.get('id'))}:{token}"))
    s = sum(leg.effective_cost for leg in legs)
    if s >= 1:
        return None
    event = Event(event_key=legs[0].event_key, category="nonsports",
                  sport="politics", home="", away="", start_utc=None,
                  start_grade="absent")
    roi = q6(1 / s - 1)
    status = "REVIEW" if roi > cfg["arb_stale_warn"] else "OK"
    return Opportunity(kind="POLITICAL", category="nonsports", event=event,
                       legs=legs, edge=q6(1 - s), stakes=[], status=status,
                       notes=f"yes+no={q6(s)}" +
                             ("; stale-warn" if status == "REVIEW" else ""))
