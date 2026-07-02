from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

OpportunityKind = Literal["ARB", "EV", "DIVERGENCE", "PARLAY"]
OpportunityStatus = Literal["OK", "REVIEW"]
# PARLAY: the synthetic basket leg that carries the whole-parlay ask/bid as one instrument.
# Its component single-leg quotes stay WINNER/TOTAL so they settle through the normal path.
MarketType = Literal["WINNER", "TOTAL", "PARLAY"]
Category = Literal["sports", "nonsports"]


@dataclass(frozen=True)
class Event:
    event_key: str
    category: Category
    sport: str
    home: str
    away: str
    # K9/D1: never invent a start. None = start unknown (non-actionable downstream); the old
    # `or datetime.now()` fallback stamped fabricated times into the ledger.
    start_utc: datetime | None
    # Spec amendment (nonsports_ai_matcher): event equivalence decided by Haiku, not a human.
    # Flows into opportunity notes and flat_bets.ai_matched; such bets are always REVIEW and
    # report in their own section, never the headline.
    ai_matched: bool = False


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
    # Wave 2 (does-well#3/weakness#2): top-of-book ladder as (cost, depth_usd) levels, best
    # first, for depth-scaled slippage and depth-capped P&L. None when not captured.
    ladder: tuple[tuple[Decimal, Decimal], ...] | None = None
    # EV#6: when the upstream book last MOVED this line (The Odds API `last_update`), used as
    # the consensus-age basis instead of our fetch wall-clock. None when the venue can't report
    # it (then callers fall back to fetched_at).
    source_updated_at: datetime | None = None
    # K12: best bid on the SAME side (per $1 payout), so a true (bid+ask)/2 mid is computable
    # later (real mid-based CLV, maker-side analysis). None when the venue can't report it.
    best_bid: Decimal | None = None

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
    edge: Decimal              # kind-specific: ARB ROI (1/S-1); EV ratio (p-c)/c
    stakes: dict[str, Decimal]
    status: OpportunityStatus
    notes: tuple[str, ...] = field(default_factory=tuple)
    fingerprint: str = ""
    # #9 comparable across kinds: probability-points edge. ARB = 1-S (margin below $1);
    # EV = p-c. Quality thresholds and cross-kind ranking use THIS, never `edge` (whose
    # units differ — a cheap EV contract inflates the ratio and was falsely quarantined).
    edge_points: Decimal | None = None
    # Phase 1 quality: None = clean; else labels such as 'suspect-edge', 'thin-book',
    # 'micro', or 'one-sided'.
    quality_flag: str | None = None
    # Phase 3 fractional-Kelly projection (never the headline stake).
    kelly_f_star: Decimal | None = None
    kelly_stake: Decimal | None = None
    # K4: skew-haircut-adjusted edge (ARB): edge_points minus skew·velocity. What sizing and
    # ranking should trust; stored as a real column, not a note.
    adj_edge: Decimal | None = None
    # K14: the model's RAW probability estimate at flag time (EV), BEFORE push/decay haircuts.
    # Calibration must judge this, not a p̂ reconstructed from the post-haircut edge.
    p_hat: Decimal | None = None

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
