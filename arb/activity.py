"""Venue-derived sport activity (SKIP_DEAD_SPORTS): the free venues' own listings are
the season calendar — no hardcoded dates. A sport is active iff any venue quote has a
known start within the lookahead window; dormant sports are skipped by detection and
by Odds API scans (the credit saver) but their quotes still persist (rule #7).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import Quote


def active_sports(quotes: list[Quote], now: datetime, lookahead_hours: int) -> set[str]:
    horizon = now + timedelta(hours=lookahead_hours)
    return {
        quote.sport
        for quote in quotes
        if quote.start_utc is not None and now < quote.start_utc <= horizon
    }
