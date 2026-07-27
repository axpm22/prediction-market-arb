"""Offline policy replay (PROMPT_REPLAY), phase R1: rebuild a Quote from a
persisted quotes row — the inverse of arb.cycle._quote_row. Read-only by
construction; this module never writes anything, anywhere.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from arb.models import Quote


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _dt(v) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def quote_from_row(row) -> Quote:
    """Inverse of arb.cycle._quote_row. home/away come from the event_key
    (`sport:date:away:home[:dhN]`) because quotes never store the venue-raw
    names; venue_side is reconcile-only and unread by detect()."""
    r = dict(row)
    parts = r["event_key"].split(":")
    away, home = (parts[2], parts[3]) if len(parts) >= 4 else ("", "")
    return Quote(
        event_key=r["event_key"], venue=r["venue"], book=r["book"],
        market_type=r["market_type"], line=r["line"], outcome_label=r["outcome"],
        side=r["side"], raw_price=_dec(r["raw_price"]), cost=_dec(r["cost"]),
        fee_cost=_dec(r["fee_cost"]), depth_usd=_dec(r["depth_usd"]),
        ladder_json=r["ladder_json"], live=bool(r["live"]), fetched_at=_dt(r["fetched_at"]),
        category=r["category"], sport=r["sport"], bid=_dec(r["bid"]),
        volume_24h=_dec(r["volume_24h"]), home=home, away=away,
        start_utc=_dt(r["start_utc"]), start_grade=r["start_grade"],
        venue_market_id=r["venue_market_id"] or "", venue_side="")
