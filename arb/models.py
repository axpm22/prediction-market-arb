from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

OpportunityKind = Literal["ARB", "EV", "DIVERGENCE"]
OpportunityStatus = Literal["OK", "REVIEW"]
MarketType = Literal["WINNER", "TOTAL"]
Category = Literal["sports", "nonsports"]


@dataclass(frozen=True)
class Event:
    event_key: str
    category: Category
    sport: str
    home: str
    away: str
    start_utc: datetime


@dataclass(frozen=True)
class Quote:
    event_key: str
    venue: Literal["oddsapi", "kalshi", "polymarket", "sx"]
    book: str
    market_type: MarketType
    line: Decimal | None
    outcome_label: str
    raw_price: Decimal
    cost: Decimal
    fee_cost: Decimal
    depth_usd: Decimal | None
    live: bool
    fetched_at: datetime
    start_utc: datetime | None = None
    home: str | None = None
    away: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    # Venue-specific re-quote hint so a stored leg can be re-priced later. For SX it is
    # the outcome side ("1"/"2") needed to pick the right maker orders; None elsewhere.
    ref: str | None = None

    @property
    def effective_cost(self) -> Decimal:
        return self.cost + self.fee_cost

    @property
    def sport(self) -> str:
        return self.event_key.split(":", 1)[0]

    @property
    def max_size_usd(self) -> Decimal | None:
        return self.depth_usd


@dataclass(frozen=True)
class Opportunity:
    kind: OpportunityKind
    category: Category
    event: Event
    legs: list[Quote]
    edge: Decimal
    stakes: dict[str, Decimal]
    status: OpportunityStatus
    notes: tuple[str, ...] = field(default_factory=tuple)
    fingerprint: str = ""
    # Phase 1 quality: None = clean; else 'suspect-edge' | 'thin-book' | 'one-sided'.
    quality_flag: str | None = None
    # Phase 3 fractional-Kelly projection (never the headline stake).
    kelly_f_star: Decimal | None = None
    kelly_stake: Decimal | None = None

    def __post_init__(self) -> None:
        if self.fingerprint:
            return
        object.__setattr__(self, "fingerprint", opportunity_fingerprint(self.kind, self.event.event_key, self.legs))


def opportunity_fingerprint(kind: str, event_key: str, legs: list[Quote]) -> str:
    parts = [
        f"{leg.venue}|{leg.book}|{leg.market_type}|{leg.line or ''}|{leg.outcome_label}"
        for leg in legs
    ]
    payload = f"{event_key}|{kind}|{'|'.join(sorted(parts))}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
