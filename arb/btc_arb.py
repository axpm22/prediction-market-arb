"""Same-oracle short-horizon BTC cross-venue arb — BUILD_PLAN Phase 4 / S3.

Unlike sports (where venues settle on different game-rule conventions), short-horizon
crypto markets settle off a PRICE INDEX, so a genuine cross-venue edge can exist — but
ONLY if both venues reference the same settlement source. Probed June 2026: Kalshi BTC
hourly markets settle off a specific index/timestamp; Polymarket uses its own oracle. They
are NOT provably identical, so every flag here stays REVIEW and is never auto-filled
(hard rule #6). The detection is the deliverable; the REVIEW gate is the honest verdict.

Matching: (asset, direction UP/DOWN, window_end_utc) within window_match_secs. Never
interpolate windows — exact end-time match only (mirrors the totals exact-line rule).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .pricing import q6


@dataclass(frozen=True)
class BtcLeg:
    venue: str
    asset: str
    direction: str        # 'UP' | 'DOWN'
    window_end_utc: datetime
    effective_cost: Decimal  # ask + fees, in the $1 unit
    oracle: str           # settlement source string (for the rulebook comparison)


@dataclass(frozen=True)
class BtcArb:
    asset: str
    window_end_utc: datetime
    up_leg: BtcLeg
    down_leg: BtcLeg
    total_cost: Decimal
    edge: Decimal
    same_oracle: bool
    status: str           # always 'REVIEW' unless same_oracle and configured trusted


def windows_match(a: datetime, b: datetime, tolerance_secs: int) -> bool:
    return abs((a - b).total_seconds()) <= tolerance_secs


def find_btc_arb(legs: list[BtcLeg], window_match_secs: int, trusted: bool) -> list[BtcArb]:
    """Pair UP(venue A) with DOWN(venue B) on the same asset+window across venues."""
    ups = [l for l in legs if l.direction == "UP"]
    downs = [l for l in legs if l.direction == "DOWN"]
    arbs: list[BtcArb] = []
    for up in ups:
        for down in downs:
            if up.venue == down.venue:
                continue  # cross-venue only
            if up.asset != down.asset:
                continue
            if not windows_match(up.window_end_utc, down.window_end_utc, window_match_secs):
                continue
            total = q6(up.effective_cost + down.effective_cost)
            if total >= Decimal("1") or total <= 0:
                continue
            same_oracle = up.oracle == down.oracle and up.oracle != ""
            # Honest gate: an arb is only OK if oracles provably match AND config trusts it.
            status = "OK" if (same_oracle and trusted) else "REVIEW"
            arbs.append(BtcArb(up.asset, up.window_end_utc, up, down, total,
                               q6(Decimal("1") / total - Decimal("1")), same_oracle, status))
    return arbs
