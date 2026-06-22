"""Portfolio-level capital exposure — audit fix #3.

Each bet is sized independently (min of depth/micro/bankroll caps), but nothing tracked
TOTAL simultaneous commitment, so the paper book could commit more than the bankroll, and
capital is siloed per venue (you can't instantly move USDC between Kalshi/Polymarket/SX).
This computes committed stake across active bets, overall and per venue, and flags when
either the book or a single venue exceeds its capital.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
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


def compute_exposure(store: Store, bankroll: Decimal, per_venue_cap: Decimal) -> Exposure:
    by_venue: dict[str, Decimal] = {}
    total = Decimal("0")
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
        venues = {str(leg.get("venue")) for leg in legs if leg.get("venue")} or {"unknown"}
        # Each venue leg ties up that venue's own capital for the bet's stake.
        for venue in venues:
            by_venue[venue] = by_venue.get(venue, Decimal("0")) + stake
    over_venue = sorted(v for v, amt in by_venue.items() if amt > per_venue_cap)
    return Exposure(total, bankroll, by_venue, total > bankroll, over_venue)


def format_exposure(exp: Exposure) -> str:
    lines = [
        f"Committed (active bets): ${exp.total_committed} / bankroll ${exp.bankroll}"
        + ("  OVER BANKROLL" if exp.over_bankroll else ""),
    ]
    for venue, amt in sorted(exp.by_venue.items()):
        flag = "  OVER VENUE CAP" if venue in exp.over_venue else ""
        lines.append(f"  {venue:12s} ${amt}{flag}")
    return "\n".join(lines)
