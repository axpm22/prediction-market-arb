from __future__ import annotations

import json
import re
import string
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from rapidfuzz import fuzz
except ModuleNotFoundError:  # pragma: no cover
    fuzz = None

ALIAS_PATH = Path("data/aliases.json")
# weakness#7: key the event date in a fixed sports-local tz, NOT UTC. A UTC calendar date
# recreates the 00:00 seam — two venues listing a late US game at 23:55 vs 00:05 UTC get
# different dates and never match. Both venues share the same absolute start, so a shared
# tz gives them the same date. Settlement already tolerates +/-1 day for the residual.
EVENT_DATE_TZ = ZoneInfo("America/New_York")
PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def load_aliases(path: Path = ALIAS_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _basic_normalize(name: str) -> str:
    name = name.lower().translate(PUNCT_TABLE)
    name = re.sub(r"\b(fc|sc|cf|the)\b", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_name(name: str, sport: str, aliases: dict[str, dict[str, str]] | None = None) -> str:
    # Venue titles prefix a tournament tag onto the first participant ("Wimbledon ATP:
    # Rafael Jodar") — team names never legitimately contain a colon, so drop everything
    # up to the last one or the event_key never joins other venues / the consensus.
    name = name.rsplit(":", 1)[-1]
    raw = _basic_normalize(name)
    aliases = aliases if aliases is not None else load_aliases()
    sport_aliases = aliases.get(sport, {})
    return sport_aliases.get(raw, raw)


# MMA legs anchor to this ET hour of the fight date (see mma_start_anchor). Late-evening keeps the
# card "upcoming" through its whole ET calendar day so the activity gate stays ACTIVE on fight day
# (a midnight anchor is already in the past by fight time → sport wrongly reads dormant). UFC main
# cards run ~6pm–1am ET, so 23:00 sits inside the card window.
MMA_ANCHOR_HOUR_ET = 23


def mma_start_anchor(et_date: date) -> datetime:
    """MMA has no reliable per-fight start both venues agree on: Polymarket's gameStartTime is
    the card-level start (~21:00Z) while Kalshi exposes only a fight *date* in the ticker plus a
    per-fight expected-conclusion time (occurrence_datetime, 7-10h later, past the ET midnight
    seam). A fighter pair fights once per card (no doubleheaders), so the fight is uniquely keyed
    by date+names — not sub-day time. Both adapters anchor MMA legs to the same ET hour of the
    shared fight date so their event_key dates match AND their starts are byte-identical (0 drift),
    which lets them survive detection's start clustering + start-drift gate instead of being split
    apart, and keeps the activity gate ACTIVE across the fight day."""
    anchor = datetime(et_date.year, et_date.month, et_date.day, MMA_ANCHOR_HOUR_ET, tzinfo=EVENT_DATE_TZ)
    return anchor.astimezone(timezone.utc)


def event_start_map(quotes, unreliable_start_venues: dict[str, list[str]] | None = None) -> dict[str, datetime]:
    """golf_slate_tennis_fix Part 1.0: per-event start = the EARLIEST start any RELIABLE venue
    claims for the event. Kalshi/SX starts are verified-reliable, so the earliest claim wins — a
    too-early start is safe (routes to the live/non-actionable lane), a too-late one is the bug
    that booked in-play tennis as pregame EV. Venues in `unreliable_start_venues` for the event's
    sport (Polymarket's nominal Gamma slot for tennis) are excluded as a start source; an event
    whose ONLY start comes from an unreliable venue therefore maps to nothing here → start unknown
    → non-actionable (E5)."""
    unreliable = unreliable_start_venues or {}
    starts: dict[str, datetime] = {}
    for quote in quotes:
        if quote.start_utc is None:
            continue
        if quote.sport in unreliable.get(quote.venue, ()):  # unreliable start source for this sport
            continue
        current = starts.get(quote.event_key)
        if current is None or quote.start_utc < current:
            starts[quote.event_key] = quote.start_utc
    return starts


def canonical_sport(odds_api_key: str, aliases: dict[str, str] | None = None) -> str:
    """Map an Odds-API sport key to the venue-canonical event_key prefix, so consensus rows
    join venue quotes (the venues key on `soccer`, the Odds API exposes `soccer_fifa_world_cup`).
    Identity for unmapped keys — `baseball_mlb` already equals its venue prefix and is unchanged.
    """
    return (aliases or {}).get(odds_api_key, odds_api_key)


def event_key(sport: str, home: str, away: str, start_utc: datetime, aliases: dict[str, dict[str, str]] | None = None) -> str:
    # #4: NO sub-day bucket. A 6h floor bucket split two venues listing the same game at
    # 17:55 vs 18:05 (and any pair straddling a 6h boundary — evening games cluster at the
    # 18:00 UTC line), silently dropping real cross-venue arbs. Key on sport+date+pair only;
    # doubleheaders (same teams, same day, hours apart) are caught by the start-drift gate
    # (suspended, not mis-bet) and settled by each leg's exact stored start time.
    names = sorted([normalize_name(home, sport, aliases), normalize_name(away, sport, aliases)])
    start = start_utc.astimezone(EVENT_DATE_TZ)
    return f"{sport}:{start.date().isoformat()}:{names[0]}:{names[1]}"


def token_ratio(left: str, right: str) -> int:
    if fuzz is not None:
        return int(fuzz.token_set_ratio(left, right))
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0
    overlap = len(left_tokens & right_tokens)
    return int(200 * overlap / (len(left_tokens) + len(right_tokens)))


def fuzzy_same_team(left: str, right: str, threshold: int) -> bool:
    # Threshold comes from config.yaml (fuzzy_match_threshold); no inline default.
    return token_ratio(left, right) >= threshold

