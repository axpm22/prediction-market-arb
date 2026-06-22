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

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .pricing import money, q6
from .store import Store


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
    """One arb per (asset, window_end): the CHEAPEST cross-venue UP+DOWN pair.

    #6 Previously this emitted every UP x DOWN combination, inflating opportunity counts
    and double-allocating the same notional. Windows are matched EXACTLY on end time (the
    module rule: never interpolate); window_match_secs is retained for signature
    compatibility but not used to widen the match.
    """
    groups: dict[tuple[str, datetime], dict[str, list[BtcLeg]]] = defaultdict(
        lambda: {"UP": [], "DOWN": []}
    )
    for l in legs:
        key = (l.asset, l.window_end_utc.astimezone(timezone.utc))
        groups[key][l.direction].append(l)
    arbs: list[BtcArb] = []
    for (asset, window_end), sides in groups.items():
        best: tuple[Decimal, BtcLeg, BtcLeg] | None = None
        for up in sides["UP"]:
            for down in sides["DOWN"]:
                if up.venue == down.venue:
                    continue  # cross-venue only
                total = q6(up.effective_cost + down.effective_cost)
                if total >= Decimal("1") or total <= 0:
                    continue
                if best is None or total < best[0]:
                    best = (total, up, down)
        if best is None:
            continue
        total, up, down = best
        same_oracle = up.oracle == down.oracle and up.oracle != ""
        # Honest gate: an arb is only OK if oracles provably match AND config trusts it.
        status = "OK" if (same_oracle and trusted) else "REVIEW"
        arbs.append(BtcArb(asset, window_end, up, down, total,
                           q6(Decimal("1") / total - Decimal("1")), same_oracle, status))
    return arbs


def persist_btc_arbs(store: Store, poll_id: int, arbs: list[BtcArb]) -> int:
    """Append ARB_BTC rows to the flat ledger; duplicates remain a single open paper bet."""
    inserted = 0
    with store.connect() as con:
        for arb in arbs:
            fingerprint = _fingerprint(arb)
            if con.execute(
                "SELECT 1 FROM flat_bets WHERE fingerprint = ? AND status = 'OPEN' LIMIT 1",
                (fingerprint,),
            ).fetchone():
                continue
            quality_flag = None if arb.status == "OK" else "oracle-review"
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, status, placed_at,
                       notes, quality_flag)
                   VALUES (?, ?, 'nonsports', 'btc_15m', 'ARB_BTC', ?, 0, ?, '1.00', ?, ?, ?,
                           'OPEN', ?, ?, ?)""",
                (
                    fingerprint,
                    poll_id,
                    int(arb.status == "OK"),
                    str(arb.edge),
                    json.dumps([_leg_json(arb.up_leg), _leg_json(arb.down_leg)]),
                    str(arb.total_cost),
                    str(money(arb.edge)),
                    datetime.now(timezone.utc).isoformat(),
                    _notes(arb),
                    quality_flag,
                ),
            )
            inserted += 1
    return inserted


def _fingerprint(arb: BtcArb) -> str:
    return (
        f"btc:{arb.asset}:{arb.window_end_utc.astimezone(timezone.utc).isoformat()}:"
        f"{arb.up_leg.venue}:UP:{arb.down_leg.venue}:DOWN"
    )


def _leg_json(leg: BtcLeg) -> dict[str, str]:
    return {
        "venue": leg.venue,
        "asset": leg.asset,
        "direction": leg.direction,
        "window_end_utc": leg.window_end_utc.astimezone(timezone.utc).isoformat(),
        "effective_cost": str(leg.effective_cost),
        "oracle": leg.oracle,
    }


def _notes(arb: BtcArb) -> str:
    oracle_note = "same-oracle" if arb.same_oracle else "oracle-mismatch"
    return f"btc-window={arb.window_end_utc.astimezone(timezone.utc).isoformat()} | {oracle_note} | {arb.status}"
