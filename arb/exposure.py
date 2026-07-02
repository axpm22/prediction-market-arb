"""Portfolio-level capital exposure — audit fix #3.

Each bet is sized independently (min of depth/micro/bankroll caps), but nothing tracked
TOTAL simultaneous commitment, so the paper book could commit more than the bankroll, and
capital is siloed per venue (you can't instantly move USDC between Kalshi/Polymarket/SX).
This computes committed stake across active bets, overall and per venue, and flags when
either the book or a single venue exceeds its capital.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .store import Store

ACTIVE = ("OPEN", "CONFIRMED")


@dataclass(frozen=True)
class Exposure:
    total_committed: Decimal
    bankroll: Decimal
    by_venue: dict[str, Decimal]
    over_bankroll: bool
    over_venue: list[str]      # venues whose committed stake exceeds their per-venue cap
    # does-well#10: capital is also locked for TIME. A 14-day-settlement bet ties up the same
    # dollar far longer than a same-day one; this is Σ stake x days-to-settlement ($-days).
    duration_weighted_usd_days: Decimal = Decimal("0")
    caps: dict[str, Decimal] = field(default_factory=dict)


def compute_exposure(store: Store, bankroll: Decimal, per_venue_cap: Decimal,
                     per_venue_caps: dict[str, Decimal] | None = None,
                     now: datetime | None = None,
                     sport_duration_hours: dict | None = None,
                     venue_settlement_lag_hours: dict | None = None) -> Exposure:
    now = now or datetime.now(timezone.utc)
    per_venue_caps = per_venue_caps or {}
    by_venue: dict[str, Decimal] = {}
    total = Decimal("0")
    duration_weighted = Decimal("0")
    with store.connect() as con:
        rows = con.execute(
            f"SELECT stake_usd, legs_json FROM flat_bets "
            f"WHERE status IN ({','.join('?' * len(ACTIVE))}) AND kind != 'DIVERGENCE'",
            ACTIVE,
        ).fetchall()
    for row in rows:
        stake = Decimal(str(row["stake_usd"] or "0"))
        if stake <= 0:
            continue
        total += stake
        try:
            legs = json.loads(row["legs_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            legs = []
        # K6: each venue is charged its own LEG's stake, not the whole bet's. An arb split
        # $0.52/$0.48 across two venues used to book $1.00 against BOTH caps (~2x reality).
        # Leg share = leg effective cost / total cost (how equalized_arb_stakes splits).
        for venue, share in _venue_stakes(legs, stake).items():
            by_venue[venue] = by_venue.get(venue, Decimal("0")) + share
        duration_weighted += stake * _days_to_settlement(legs, now, sport_duration_hours,
                                                         venue_settlement_lag_hours)
    caps = {v: per_venue_caps.get(v, per_venue_cap) for v in by_venue}
    over_venue = sorted(v for v, amt in by_venue.items() if amt > caps[v])
    return Exposure(total, bankroll, by_venue, total > bankroll, over_venue,
                    duration_weighted_usd_days=Decimal(str(round(duration_weighted, 2))), caps=caps)


def _venue_stakes(legs: list[dict], stake: Decimal) -> dict[str, Decimal]:
    """K6: apportion a bet's stake to venues by each leg's cost share (the same proportion
    equalized_arb_stakes staked them at). Single-venue bets get the full stake."""
    costs: dict[str, Decimal] = {}
    for leg in legs:
        venue = str(leg.get("venue") or "unknown")
        try:
            cost = Decimal(str(leg.get("cost") or "0")) + Decimal(str(leg.get("fee_cost") or "0"))
        except Exception:  # noqa: BLE001 — malformed leg -> charge nothing extra for it
            cost = Decimal("0")
        costs[venue] = costs.get(venue, Decimal("0")) + max(cost, Decimal("0"))
    if len(costs) <= 1:
        return {venue: stake for venue in (costs or {"unknown": None})}
    total_cost = sum(costs.values(), Decimal("0"))
    if total_cost <= 0:
        return {venue: stake for venue in costs}  # unknown split -> conservative full charge
    return {venue: (stake * cost / total_cost).quantize(Decimal("0.01")) for venue, cost in costs.items()}


def _days_to_settlement(legs: list[dict], now: datetime,
                        sport_duration_hours: dict | None = None,
                        venue_settlement_lag_hours: dict | None = None) -> Decimal:
    """K6/C4: days capital is locked — max over legs of (start + game duration + that venue's
    payout lag), matching detection's capital charge. Falls back to bare start when the
    duration/lag tables aren't supplied."""
    finals = []
    for leg in legs:
        raw = leg.get("start_utc")
        if not raw:
            continue
        try:
            start = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
        if sport_duration_hours is not None and venue_settlement_lag_hours is not None:
            sport = str(leg.get("event_key") or "").split(":", 1)[0]
            dur = sport_duration_hours.get(sport, Decimal("4"))
            lag = venue_settlement_lag_hours.get(str(leg.get("venue")), Decimal("24"))
            start = start + timedelta(hours=float(dur) + float(lag))
        finals.append(start)
    if not finals:
        return Decimal("0")
    days = (max(finals) - now).total_seconds() / 86400.0
    return Decimal(str(max(0.0, days)))


def format_exposure(exp: Exposure) -> str:
    lines = [
        f"Committed (active bets): ${exp.total_committed} / bankroll ${exp.bankroll}"
        + ("  OVER BANKROLL" if exp.over_bankroll else ""),
        f"Duration-weighted exposure: {exp.duration_weighted_usd_days} $-days locked",
    ]
    for venue, amt in sorted(exp.by_venue.items()):
        cap = exp.caps.get(venue)
        flag = "  OVER VENUE CAP" if venue in exp.over_venue else ""
        cap_str = f" / cap ${cap}" if cap is not None else ""
        lines.append(f"  {venue:12s} ${amt}{cap_str}{flag}")
    return "\n".join(lines)
