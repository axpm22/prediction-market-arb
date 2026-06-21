"""Flash verification of fresh ARB flags — re-quote the exact legs seconds after the
flag instead of waiting ~4 minutes for the next full sweep.

Appends one row per checked bet (append-only, like everything in flat_bets):
  VERIFIED  — every leg re-quoted and the summed cost still < $1 (edge recomputed)
  VANISHED  — a leg's book emptied or the recomputed sum >= $1 (edge shows the damage)
  UNVERIFIABLE — a leg can't be fast-checked (SX needs market metadata we don't store)

This bounds staleness to seconds. It says nothing about settlement-rule risk — only
reading both venues' rulebooks (trusted_pairs) covers that.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import requests

from arb.pricing import kalshi_fee_cost, q6
from arb.store import Store
from arb.venues.kalshi import BASE_URL as KALSHI_URL
from arb.venues.polymarket import CLOB_URL, _best_ask

HEADERS = {"User-Agent": "arb-paper-scanner/0.1 (public read-only)"}

# A fetcher takes a leg dict and returns the leg's CURRENT effective ask (cost+fee),
# or None when the book is empty/gone.
Fetcher = Callable[[dict[str, Any]], Decimal | None]


def unverified_arb_bets(store: Store, now: datetime, max_age_secs: int) -> list[sqlite3.Row]:
    cutoff = (now - timedelta(seconds=max_age_secs)).isoformat()
    with store.connect() as con:
        return list(
            con.execute(
                """
                SELECT * FROM flat_bets b
                 WHERE b.kind = 'ARB' AND b.status = 'OPEN' AND b.placed_at >= ?
                   AND b.notes NOT LIKE '%research-unverifiable%'
                   AND NOT EXISTS (SELECT 1 FROM flat_bets v
                                    WHERE v.reappearance_of = b.id
                                      AND v.status IN ('VERIFIED', 'VANISHED', 'UNVERIFIABLE'))
                 ORDER BY b.id
                """,
                (cutoff,),
            )
        )


def reverify_bet(bet: sqlite3.Row, fetchers: dict[str, Fetcher]) -> tuple[str, Decimal | None, str]:
    """Re-quote every leg; return (status, atomic_edge, note).

    The note also carries the SEQUENTIAL edge: leg one held at its detection price, the
    remaining legs at re-quote prices — simulating filling the first leg instantly and
    the rest after real latency. atomic minus sequential is the measured leg-risk cost.
    """
    legs = json.loads(bet["legs_json"])
    requoted: list[Decimal] = []
    for leg in legs:
        fetcher = fetchers.get(str(leg["venue"]))
        if fetcher is None:
            return "UNVERIFIABLE", None, f"no fast re-quote for venue {leg['venue']}"
        current = fetcher(leg)
        if current is None:
            return "VANISHED", None, f"{leg['venue']} leg book empty on re-quote"
        requoted.append(current)
    atomic_total = sum(requoted, Decimal("0"))
    if atomic_total <= 0:
        return "UNVERIFIABLE", None, "re-quote returned non-positive cost"
    atomic_edge = q6(Decimal("1") / atomic_total - Decimal("1"))
    # Sequential edge under EVERY leg ordering (hold leg i at detection price, re-quote
    # the rest); headline the WORST one — leg risk must not get to pick its best case.
    sequential_edges: list[Decimal] = []
    for held in range(len(legs)):
        held_cost = Decimal(str(legs[held]["cost"])) + Decimal(str(legs[held]["fee_cost"]))
        total = held_cost + sum(c for i, c in enumerate(requoted) if i != held)
        if total > 0:
            sequential_edges.append(q6(Decimal("1") / total - Decimal("1")))
    seq_worst = min(sequential_edges) if sequential_edges else None
    seq_best = max(sequential_edges) if sequential_edges else None
    lag_secs = (datetime.now(timezone.utc) - datetime.fromisoformat(str(bet["placed_at"]))).total_seconds()
    note = (
        f"re-quoted sum {q6(atomic_total)}; lag={lag_secs:.1f}s "
        f"atomic={atomic_edge} seq_worst={seq_worst} seq_best={seq_best}"
    )
    if atomic_total < Decimal("1"):
        return "VERIFIED", atomic_edge, note
    return "VANISHED", atomic_edge, note + " (>=1)"


def run_verify_once(store: Store, config, vcfg: dict[str, Any], fetchers: dict[str, Fetcher] | None = None,
                    now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    fetchers = fetchers if fetchers is not None else live_fetchers(config)
    checked = 0
    bets = unverified_arb_bets(store, now, int(vcfg["max_bet_age_secs"]))
    if not bets:
        return 0
    with store.connect() as con:
        for bet in bets:
            status, edge, note = reverify_bet(bet, fetchers)
            con.execute(
                """
                INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind,
                                      trusted, live, edge, stake_usd, legs_json, status, placed_at, notes)
                VALUES (?, ?, ?, ?, ?, 'ARB', ?, ?, ?, '0', ?, ?, ?, ?)
                """,
                (
                    bet["fingerprint"], bet["id"], bet["opened_poll_id"], bet["category"], bet["sport"],
                    bet["trusted"], bet["live"], str(edge) if edge is not None else bet["edge"],
                    bet["legs_json"], status, now.isoformat(), f"flash-verify: {note}",
                ),
            )
            checked += 1
    return checked


def live_fetchers(config) -> dict[str, Fetcher]:
    return {
        "kalshi": lambda leg: _kalshi_current_ask(leg, config.kalshi_fee_rate),
        "polymarket": _polymarket_current_ask,
        # SX legs now carry an outcome-side hint (leg["ref"]) so we can re-fetch orders
        # and re-price the exact side — see arb.venues.sx.requote_sx_leg.
        "sx": lambda leg: _sx_current_ask(leg, config.sx_fee_rate),
    }


def _sx_current_ask(leg: dict[str, Any], fee_rate: Decimal | None) -> Decimal | None:
    from arb.venues.sx import requote_sx_leg

    return requote_sx_leg(str(leg["book"]), leg.get("ref"), fee_rate)


def _kalshi_current_ask(leg: dict[str, Any], fee_rate: Decimal) -> Decimal | None:
    response = requests.get(f"{KALSHI_URL}/markets/{leg['book']}/orderbook", timeout=30, headers=HEADERS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    book = response.json().get("orderbook_fp") or response.json().get("orderbook") or {}
    # Buying YES consumes NO bids (ask = 1 - best NO bid) and vice versa.
    side_is_no = leg.get("market_type") == "TOTAL" and leg.get("outcome_label") == "UNDER"
    opposing = book.get("yes_dollars" if side_is_no else "no_dollars") or []
    if not opposing:
        return None
    best_bid = max(Decimal(str(level[0])) for level in opposing)
    price = Decimal("1") - best_bid
    if not Decimal("0") < price < Decimal("1"):
        return None
    return q6(price + kalshi_fee_cost(price, fee_rate))


def _polymarket_current_ask(leg: dict[str, Any]) -> Decimal | None:
    response = requests.get(f"{CLOB_URL}/book", params={"token_id": leg["book"]}, timeout=30, headers=HEADERS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    best = _best_ask(response.json().get("asks", []))
    return None if best is None else q6(best[0])
