"""Venue-resolution reconciliation — audit fix #1.

A cross-venue ARB is "locked" only if BOTH venues resolve the leg the same way the score
implies. Scores alone can't see a settlement mismatch (extra innings, void, different
oracle), so a settled ARB's realized P&L was always just copied from the provisional
locked value — the single biggest unmeasured risk in the book.

This module compares each leg's venue-actual resolution against the clean-arb expectation
and computes the TRUE realized P&L. When they disagree it appends a CORRECTED row (the
append-only ledger never edits) and the disagreement is the settler-error signal.

leg payout per $1 staked-as-cost:
  WON  -> $1 collected
  LOST -> $0
  VOID -> the leg's own cost refunded (no win, no loss on that leg)
A clean two-way arb has exactly one WON leg and zero VOID; realized then equals the
provisional 1/Σcost - 1. Anything else is a mismatch.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .pricing import money
from .store import Store


@dataclass(frozen=True)
class Reconciliation:
    realized_pnl: Decimal
    agrees: bool          # realized == provisional (clean lock held)
    detail: str


def reconcile_arb(leg_results: list[tuple[Decimal, str]], provisional_pnl: Decimal) -> Reconciliation:
    """leg_results: (leg_cost, result) per leg, result in {WON,LOST,VOID}. Returns the
    true realized P&L on $1 total staked across the basket and whether it matches the lock."""
    total_cost = sum((c for c, _ in leg_results), Decimal("0"))
    if total_cost <= 0:
        return Reconciliation(Decimal("0.00"), True, "no-cost")
    payout = Decimal("0")
    wins = voids = 0
    for cost, result in leg_results:
        if result == "WON":
            payout += Decimal("1")
            wins += 1
        elif result == "VOID":
            payout += cost  # refund this leg's share
            voids += 1
    # Normalize to the $1-total convention the ledger uses for arbs.
    realized = money(payout / total_cost - Decimal("1"))
    agrees = (wins == 1 and voids == 0 and realized == provisional_pnl)
    if wins == 0 and voids == 0:
        detail = "MISMATCH: no leg won (both venues resolved against the basket)"
    elif wins > 1:
        detail = f"MISMATCH: {wins} legs won (venues resolved inconsistently)"
    elif voids:
        detail = f"void on {voids} leg(s) — partial refund"
    elif agrees:
        detail = "agree: lock held"
    else:
        detail = "realized differs from provisional"
    return Reconciliation(realized, agrees, detail)


def _resolver_for(leg_resolver, venue: str):
    """Audit A2: resolvers compose by venue. `leg_resolver` is either a single callable
    (applied to every leg — back-compat) or a {venue: callable} mapping that dispatches
    per leg so a kalshi+poly arb resolves its kalshi leg with the kalshi resolver and its
    poly leg with the poly resolver. A venue with no resolver yields None (unresolved)."""
    if isinstance(leg_resolver, dict):
        return leg_resolver.get(venue)
    return leg_resolver


def reconcile_settlements(store: Store, leg_resolver) -> dict[str, int]:
    """For each settled ARB lacking a reconciliation, fetch venue-actual leg results via
    per-venue resolvers `(leg) -> 'WON'|'LOST'|'VOID'|None` and append a RECONCILED/CORRECTED
    row. `leg_resolver` is a single callable OR a {venue: callable} mapping (audit A2). A bet
    is "unresolved" only when *its own* leg's venue resolver returns None — no longer skipped
    just because one venue lacks a resolver. Returns {checked, agree, corrected, skipped}."""
    counts = {"checked": 0, "agree": 0, "corrected": 0, "skipped": 0}
    with store.connect() as con:
        bets = list(con.execute(
            """
            SELECT b.* FROM flat_bets b
             WHERE b.kind='ARB' AND b.status='SETTLED' AND b.result='RESOLVED'
               AND NOT EXISTS (SELECT 1 FROM flat_bets r
                                WHERE r.reappearance_of=b.id AND r.status IN ('RECONCILED','CORRECTED'))
             ORDER BY b.id
            """
        ))
        for bet in bets:
            legs = json.loads(bet["legs_json"] or "[]")
            leg_results: list[tuple[Decimal, str]] = []
            unresolved = False
            for leg in legs:
                resolver = _resolver_for(leg_resolver, str(leg.get("venue")))
                res = resolver(leg) if resolver is not None else None
                if res is None:
                    unresolved = True
                    break
                cost = Decimal(str(leg.get("cost", "0"))) + Decimal(str(leg.get("fee_cost", "0")))
                leg_results.append((cost, res))
            if unresolved or not leg_results:
                counts["skipped"] += 1
                continue
            counts["checked"] += 1
            provisional = Decimal(str(bet["pnl_optimistic"] or "0"))
            recon = reconcile_arb(leg_results, provisional)
            status = "RECONCILED" if recon.agrees else "CORRECTED"
            counts["agree" if recon.agrees else "corrected"] += 1
            _append_recon_row(con, bet, recon, status)
        # Audit A2: any settled arb still without a venue cross-check carries an unverified
        # provisional lock — surface the count so the headline can label it provisional.
        counts["provisional"] = provisional_arb_count(con)
    return counts


def provisional_arb_count(con: sqlite3.Connection) -> int:
    """Settled RESOLVED arbs whose realized P&L is still the unverified provisional lock
    (no RECONCILED/CORRECTED child row yet). Until this is 0, arb realized P&L is provisional."""
    row = con.execute(
        """
        SELECT COUNT(*) FROM flat_bets b
         WHERE b.kind='ARB' AND b.status='SETTLED' AND b.result='RESOLVED'
           AND NOT EXISTS (SELECT 1 FROM flat_bets r
                            WHERE r.reappearance_of=b.id AND r.status IN ('RECONCILED','CORRECTED'))
        """
    ).fetchone()
    return int(row[0]) if row else 0


def default_resolvers() -> dict:
    """Audit A2: the live per-venue resolver map. Each is a public, unauthenticated
    settled-resolution read mirroring kalshi_leg_result's WON|LOST|VOID|None contract.
    SX stays in the map but fails closed until its settled-market field set is verified
    live (see CRITICAL_AND_HIGH.md A2), so it can only ever skip, never fake a lock."""
    from .venues.kalshi import kalshi_leg_result
    from .venues.polymarket import polymarket_leg_result
    from .venues.sx import sx_leg_result

    return {"kalshi": kalshi_leg_result, "polymarket": polymarket_leg_result, "sx": sx_leg_result}


def _append_recon_row(con: sqlite3.Connection, bet: sqlite3.Row, recon: Reconciliation, status: str) -> None:
    note = f"venue-reconcile: {recon.detail} | realized={recon.realized_pnl} provisional={bet['pnl_optimistic']}"
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind,
                              trusted, live, edge, stake_usd, legs_json, optimistic_cost,
                              pnl_optimistic, status, placed_at, settled_at, result, notes)
        VALUES (?, ?, ?, ?, ?, 'ARB', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bet["fingerprint"], bet["id"], bet["opened_poll_id"], bet["category"], bet["sport"],
            bet["trusted"], bet["live"], bet["edge"], bet["stake_usd"], bet["legs_json"],
            bet["optimistic_cost"], str(recon.realized_pnl), status,
            bet["placed_at"], datetime.now(timezone.utc).isoformat(),
            "AGREE" if recon.agrees else "SETTLER-ERROR", note,
        ),
    )
