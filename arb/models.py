"""Canonical data model (SPEC §7.1). Everything in cost per $1 payout, Decimal."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

D6 = Decimal("0.000001")


def q6(x: Decimal) -> Decimal:
    """Quantize at store/compare boundaries only, never mid-chain (v3.4)."""
    return Decimal(x).quantize(D6, rounding=ROUND_HALF_EVEN)


# enums (pinned)
MARKET_TYPES = ("WINNER", "TOTAL")
SIDES = ("YES", "NO")
LANES = ("clean", "review", "invalid")
QUALITY_FLAGS = ("none", "empty_book", "one_sided", "match_suspect",
                 "stale_consensus", "edge_implausible", "zombie")
START_GRADES = ("verified", "nominal", "absent")
# RULE 1 (v3.5): ARB is same-venue only; cross-venue is XSPREAD, never called arb.
KINDS = ("ARB", "XSPREAD", "EV", "DIVERGENCE", "NEGRISK", "POLITICAL", "BTC", "PARLAY")
LOCKED_KINDS = ("ARB", "XSPREAD", "NEGRISK", "POLITICAL", "BTC")  # locked at entry
RESOLUTION_SETTLED_KINDS = ("EV", "NEGRISK", "BTC")  # instant-settle tripwire applies


@dataclass
class Event:
    event_key: str
    category: str            # sports | nonsports
    sport: str
    home: str
    away: str
    start_utc: datetime | None
    start_grade: str         # verified > nominal > absent (§11.3)
    gamepk: int | None = None  # MLB gamePk — doubleheader disambiguator (§8.2, H3)


@dataclass
class Quote:
    event_key: str
    venue: str
    book: str                # sportsbook for oddsapi rows; = venue for venue-native rows
    market_type: str         # WINNER | TOTAL
    line: str | None         # Decimal-string; NEVER float in keys (§8.3)
    outcome_label: str       # normalized team / DRAW / OVER / UNDER — the proposition
    side: str                # YES | NO — which side of it this quote buys (v3.4)
    raw_price: Decimal
    cost: Decimal            # implied prob of the executable ask
    fee_cost: Decimal
    depth_usd: Decimal | None
    ladder_json: str | None
    live: bool
    fetched_at: datetime
    category: str = "sports"
    sport: str = ""
    bid: Decimal | None = None       # for mids/markouts/zombie guard
    volume_24h: Decimal | None = None
    # venue-raw event identity; matching (§8) normalizes these and stamps event_key
    home: str = ""
    away: str = ""
    start_utc: datetime | None = None
    start_grade: str = "nominal"     # verified > nominal > absent (§11.3)
    venue_market_id: str = ""        # ticker / conditionId+token / marketHash
    venue_side: str = ""             # side AT THE VENUE — complement_map never touches
                                     # this; reconcile maps venue results through it

    @property
    def effective_cost(self) -> Decimal:
        return self.cost + self.fee_cost


@dataclass
class Opportunity:
    kind: str
    category: str
    event: Event
    legs: list               # list[Quote]
    edge: Decimal            # probability points per $1 payout (§7.1), NEVER a ROI
    stakes: list             # Decimal per leg
    status: str              # OK | REVIEW
    notes: str
    fingerprint: str = ""
    position_key: str = ""
    live: bool = False    # from a live cycle: review-lane forever (classify)

    def __post_init__(self):
        if not self.fingerprint:
            self.fingerprint = fingerprint(self.event.event_key, self.kind, self.legs)


def fingerprint(event_key: str, kind: str, legs: list) -> str:
    """sha1(event_key | kind | sorted per-leg identity) — prices excluded (§7.1).
    side included post-complement-map (v3.4)."""
    leg_ids = sorted(
        f"{q.venue}|{q.book}|{q.market_type}|{q.line or ''}|{q.outcome_label}|{q.side}"
        for q in legs)
    payload = "|".join([event_key, kind] + leg_ids)
    return hashlib.sha1(payload.encode()).hexdigest()


def position_key(kind: str, sport: str, event_key: str, market_type: str,
                 line: str | None, outcome: str | None = None, side: str | None = None,
                 category: str = "sports", basket_sha: str | None = None) -> str:
    """The economic position, venue-independent, defined per kind (§7.1)."""
    if kind in ("ARB", "XSPREAD"):
        return "|".join([sport, event_key, market_type, line or "", "COVER"])
    if kind == "NEGRISK":
        return "|".join([category, event_key, "BASKET", basket_sha or ""])
    parts = [sport, event_key, outcome or "", market_type, line or ""]
    if side is not None:  # complement-map illegal (soccer, integer lines): tuple gains side
        parts.append(side)
    return "|".join(parts)
