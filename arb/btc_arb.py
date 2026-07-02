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


def _cluster_windows(legs: list[BtcLeg], tol_secs: int) -> list[list[BtcLeg]]:
    """B4 — group a strike's legs into windows by proximity: legs whose end-times gap by more
    than tol_secs are different windows. This matches the true settlement instant within
    window_match_secs instead of a minute-string key (which missed 1-minute representational
    gaps between Kalshi close_time and Polymarket endDate). BTC windows are ~hourly, so chaining
    is not a practical concern at a few-second tolerance."""
    ordered = sorted(legs, key=lambda l: l.window_end_utc.astimezone(timezone.utc))
    clusters: list[list[BtcLeg]] = []
    current: list[BtcLeg] = []
    for leg in ordered:
        end = leg.window_end_utc.astimezone(timezone.utc)
        if current and (end - current[-1].window_end_utc.astimezone(timezone.utc)).total_seconds() > tol_secs:
            clusters.append(current)
            current = []
        current.append(leg)
    if current:
        clusters.append(current)
    return clusters


def find_btc_arb(legs: list[BtcLeg], window_match_secs: int, trusted: bool) -> list[BtcArb]:
    """One arb per (strike, window cluster): the CHEAPEST cross-venue UP+DOWN pair.

    #6 Previously this emitted every UP x DOWN combination, inflating opportunity counts and
    double-allocating the same notional. B4: the strike is matched EXACTLY (the asset key) while
    the window is matched within window_match_secs, so genuinely simultaneous windows are not
    missed by a representational minute gap, and a different strike is never paired.
    """
    by_strike: dict[str, list[BtcLeg]] = defaultdict(list)
    for l in legs:
        by_strike[l.asset].append(l)
    arbs: list[BtcArb] = []
    for asset, strike_legs in by_strike.items():
        for cluster in _cluster_windows(strike_legs, window_match_secs):
            best: tuple[Decimal, BtcLeg, BtcLeg] | None = None
            for up in (l for l in cluster if l.direction == "UP"):
                for down in (l for l in cluster if l.direction == "DOWN"):
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
            window_end = min(up.window_end_utc, down.window_end_utc).astimezone(timezone.utc)
            same_oracle = up.oracle == down.oracle and up.oracle != ""
            # Honest gate: an arb is only OK if oracles provably match AND config trusts it.
            status = "OK" if (same_oracle and trusted) else "REVIEW"
            arbs.append(BtcArb(asset, window_end, up, down, total,
                               q6(Decimal("1") / total - Decimal("1")), same_oracle, status))
    return arbs


def find_btc_intramarket_arb(legs: list[BtcLeg]) -> list[BtcArb]:
    """B1 — the only FILLABLE BTC edge: a single-venue YES+NO lock.

    On ONE market (same venue, same asset = strike@window), buying UP (yes ask) AND DOWN
    (no ask) for a combined cost < $1 guarantees a $1 payout — same oracle, same window, no
    cross-venue rulebook risk. So `same_oracle=True` and `status='OK'` (this is the
    political/negrisk intramarket pattern applied to BTC). One arb per (venue, asset, window).
    """
    by_market: dict[tuple[str, str, datetime], dict[str, BtcLeg]] = defaultdict(dict)
    for leg in legs:
        by_market[(leg.venue, leg.asset, leg.window_end_utc.astimezone(timezone.utc))][leg.direction] = leg
    arbs: list[BtcArb] = []
    for (_venue, asset, window_end), sides in by_market.items():
        up, down = sides.get("UP"), sides.get("DOWN")
        if up is None or down is None:
            continue
        total = q6(up.effective_cost + down.effective_cost)
        if total >= Decimal("1") or total <= 0:
            continue
        arbs.append(BtcArb(asset, window_end, up, down, total,
                           q6(Decimal("1") / total - Decimal("1")), same_oracle=True, status="OK"))
    return arbs


def _asset_strike(asset: str) -> tuple[str, Decimal | None]:
    symbol, _, strike = asset.partition("@")
    try:
        return symbol, Decimal(strike)
    except (ArithmeticError, ValueError):
        return symbol, None


def find_btc_ladder_arb(legs: list[BtcLeg]) -> list[BtcArb]:
    """B1 ladder extension — a single-venue lock from an inconsistent threshold ladder.

    By no-arbitrage P(BTC≥X) is monotone DECREASING in X, so a lower-strike YES must cost at
    least a higher-strike YES. When the ladder inverts, buying UP at the LOWER strike + DOWN at
    the HIGHER strike (same venue, same window, same oracle) is a dominating position: it pays
    $1 in every outcome and $2 in the [X_low, X_high) band, so a combined cost < $1 is a
    guaranteed profit — fillable, status OK (no cross-venue rulebook risk). One arb per
    (venue, symbol, window): the cheapest such pair.
    """
    groups: dict[tuple[str, str, datetime], dict[str, dict[Decimal, BtcLeg]]] = defaultdict(
        lambda: {"UP": {}, "DOWN": {}}
    )
    for leg in legs:
        symbol, strike = _asset_strike(leg.asset)
        if strike is None:
            continue
        key = (leg.venue, symbol, leg.window_end_utc.astimezone(timezone.utc))
        groups[key][leg.direction][strike] = leg
    arbs: list[BtcArb] = []
    for (_venue, _symbol, window_end), sides in groups.items():
        best: tuple[Decimal, BtcLeg, BtcLeg] | None = None
        for low_strike, up in sides["UP"].items():           # UP at the LOWER strike
            for high_strike, down in sides["DOWN"].items():   # DOWN at the HIGHER strike
                if low_strike >= high_strike:
                    continue
                total = q6(up.effective_cost + down.effective_cost)
                if total >= Decimal("1") or total <= 0:
                    continue
                if best is None or total < best[0]:
                    best = (total, up, down)
        if best is None:
            continue
        total, up, down = best
        # Distinct asset label (both strikes) so the ladder lock never collides with an
        # intramarket (single-strike) arb's fingerprint.
        ladder_asset = f"{up.asset}/{down.asset.split('@')[-1]}-ladder"
        arbs.append(BtcArb(ladder_asset, window_end, up, down, total,
                           q6(Decimal("1") / total - Decimal("1")), same_oracle=True, status="OK"))
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
