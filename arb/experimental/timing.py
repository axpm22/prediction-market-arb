"""Break-window timing split (SPEC §1 timing) — pure function, wired in later.

The current pipeline lumps everything started into one excluded in-play bucket.
This three-way split is the upgrade path: in-break quotes become detectable in a
separate live section. Never blend in-break and pregame quotes in one detection
pass — a halftime price against a pregame price is a fake arb by construction.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from arb.models import Quote


def split_by_timing(
    quotes: list[Quote],
    break_windows: dict[str, list[int]],
    now: datetime,
) -> tuple[list[Quote], list[Quote], list[Quote]]:
    """Return (pregame, in_break, in_play); started quotes are marked live=True.

    break_windows maps sport -> [minutes_after_start, window_length_minutes].
    """
    pregame: list[Quote] = []
    in_break: list[Quote] = []
    in_play: list[Quote] = []
    for quote in quotes:
        if quote.start_utc is None or quote.start_utc > now:
            pregame.append(quote)
            continue
        live = quote if quote.live else replace(quote, live=True)
        window = break_windows.get(quote.sport)
        if window and _within_window(quote.start_utc, window, now):
            in_break.append(live)
        else:
            in_play.append(live)
    return pregame, in_break, in_play


def _within_window(start: datetime, window: list[int], now: datetime) -> bool:
    offset_minutes, length_minutes = int(window[0]), int(window[1])
    opens = start + timedelta(minutes=offset_minutes)
    return opens <= now <= opens + timedelta(minutes=length_minutes)
