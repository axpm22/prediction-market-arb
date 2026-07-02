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

from arb.pricing import q6
from arb.store import Store
from arb.venues.kalshi import BASE_URL as KALSHI_URL, _ladder_at_ask
from arb.venues.polymarket import CLOB_URL, _best_ask, polymarket_ladder
from arb.venues.slippage import leg_fee_cost

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


def reverify_bet(bet: sqlite3.Row, fetchers: dict[str, Fetcher]) -> tuple[str, Decimal | None, str, Decimal | None, float | None]:
    """Re-quote every leg; return (status, atomic_edge, note, requote_sum, lag_secs).

    The note also carries the SEQUENTIAL edge: leg one held at its detection price, the
    remaining legs at re-quote prices — simulating filling the first leg instantly and
    the rest after real latency. atomic minus sequential is the measured leg-risk cost.
    """
    legs = json.loads(bet["legs_json"])
    requoted: list[Decimal] = []
    for leg in legs:
        fetcher = fetchers.get(str(leg["venue"]))
        if fetcher is None:
            return "UNVERIFIABLE", None, f"no fast re-quote for venue {leg['venue']}", None, None
        current = fetcher(leg)
        if current is None:
            return "VANISHED", None, f"{leg['venue']} leg book empty on re-quote", None, None
        requoted.append(current)
    atomic_total = sum(requoted, Decimal("0"))
    if atomic_total <= 0:
        return "UNVERIFIABLE", None, "re-quote returned non-positive cost", None, None
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
        return "VERIFIED", atomic_edge, note, q6(atomic_total), lag_secs
    return "VANISHED", atomic_edge, note + " (>=1)", q6(atomic_total), lag_secs


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
            status, edge, note, requote_sum, lag_secs = reverify_bet(bet, fetchers)
            # K5: the re-quote price/lag are first-class columns (the R1 confirm source),
            # not regex-parseable prose; the note stays for humans.
            con.execute(
                """
                INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind,
                                      trusted, live, edge, stake_usd, legs_json, status, placed_at, notes,
                                      requote_sum, requote_lag_secs)
                VALUES (?, ?, ?, ?, ?, 'ARB', ?, ?, ?, '0', ?, ?, ?, ?, ?, ?)
                """,
                (
                    bet["fingerprint"], bet["id"], bet["opened_poll_id"], bet["category"], bet["sport"],
                    bet["trusted"], bet["live"], str(edge) if edge is not None else bet["edge"],
                    bet["legs_json"], status, now.isoformat(), f"flash-verify: {note}",
                    None if requote_sum is None else str(requote_sum),
                    None if lag_secs is None else round(lag_secs, 1),
                ),
            )
            checked += 1
    return checked


def live_fetchers(config) -> dict[str, Fetcher]:
    # K1/K2: every fetcher prices through the SAME leg_fee_cost the entry adapters use
    # (correct fee basis per venue, SX win fee, ladder-scaled slippage), so a re-quote at
    # the same raw price equals the adapter's effective_cost exactly.
    return {
        "kalshi": lambda leg: _kalshi_current_ask(leg, config),
        "polymarket": lambda leg: _polymarket_current_ask(leg, config),
        # SX legs now carry an outcome-side hint (leg["ref"]) so we can re-fetch orders
        # and re-price the exact side — see arb.venues.sx.requote_sx_leg.
        "sx": lambda leg: _sx_current_ask(leg, config),
    }


def _sx_current_ask(leg: dict[str, Any], config) -> Decimal | None:
    from arb.venues.sx import requote_sx_leg

    return requote_sx_leg(str(leg["book"]), leg.get("ref"), config.sx_fee_rate,
                          config.taker_price_buffer, win_fee_rate=config.sx_win_fee_rate,
                          slippage_model=config.taker_slippage_model,
                          slippage_ref_stake=config.slippage_ref_stake_usd)


def _kalshi_current_ask(leg: dict[str, Any], config) -> Decimal | None:
    response = requests.get(f"{KALSHI_URL}/markets/{leg['book']}/orderbook", timeout=30, headers=HEADERS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    # Buying YES consumes NO bids (ask = 1 - best NO bid) and vice versa; _ladder_at_ask is
    # the adapter's own ladder builder, so price + slippage match entry pricing exactly.
    side_is_no = leg.get("market_type") == "TOTAL" and leg.get("outcome_label") == "UNDER"
    ladder = _ladder_at_ask(response.json(), "no" if side_is_no else "yes")
    if not ladder:
        return None
    price = ladder[0][0]
    if not Decimal("0") < price < Decimal("1"):
        return None
    return q6(price + leg_fee_cost("kalshi", price, config.kalshi_fee_rate, ladder,
                                   config.taker_slippage_model, config.slippage_ref_stake_usd,
                                   config.taker_price_buffer))


def _polymarket_current_ask(leg: dict[str, Any], config) -> Decimal | None:
    response = requests.get(f"{CLOB_URL}/book", params={"token_id": leg["book"]}, timeout=30, headers=HEADERS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    asks = response.json().get("asks", [])
    best = _best_ask(asks)
    if best is None:
        return None
    ladder = polymarket_ladder(asks)
    return q6(best[0] + leg_fee_cost("polymarket", best[0], config.polymarket_sports_fee_rate,
                                     ladder, config.taker_slippage_model,
                                     config.slippage_ref_stake_usd, config.taker_price_buffer))
