"""Bet-by-bet focused rotation — same bet, different books, at one time.

The wide daemon sweep is venue-by-venue, so a cross-venue comparison can span minutes.
This process inverts the loop: it reads the cross-venue bet map out of the sweep's own
recent persisted quotes, then walks event by event, re-quoting one game's books on every
venue back-to-back (seconds apart) and detecting on those simultaneous prices.

Tracker interaction is deliberately one-way: focus OPENs new fingerprints and CONFIRMs
ones the daemon already has open (a confirmation at simultaneous prices is the strongest
evidence the ledger records) — it never closes anything; lifetime closes stay owned by
the wide sweep.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import requests

from arb.arbitrage import detect_divergences, detect_true_arbs, split_match_suspects
from arb.models import Opportunity, Quote
from arb.pricing import kalshi_fee_cost, q6
from arb.store import Store
from arb.tracker import _active_rows, _insert_tracker_row
from arb.venues.kalshi import BASE_URL as KALSHI_URL
from arb.venues.kalshi import _depth_at_ask
from arb.venues.polymarket import CLOB_URL, _best_ask

HEADERS = {"User-Agent": "arb-paper-scanner/0.1 (public read-only)"}
KALSHI_THROTTLE_SECS = 0.15

# A fetcher returns the leg's current (price, depth_usd) or None when the book is gone.
Fetcher = Callable[[dict[str, Any]], tuple[Decimal, Decimal | None] | None]


def build_bet_map(store: Store, now: datetime, max_age_mins: int, sports: list[str]) -> dict[str, list[Any]]:
    """event_key -> freshest stored leg rows, for pregame events priced on >= 2 venues."""
    cutoff = (now - timedelta(minutes=max_age_mins)).isoformat()
    with store.connect() as con:
        rows = list(
            con.execute(
                f"""
                SELECT * FROM quotes
                 WHERE fetched_at >= ? AND live = 0
                   AND venue IN ('kalshi', 'polymarket')
                   AND sport IN ({','.join('?' * len(sports))})
                 ORDER BY id
                """,
                [cutoff, *sports],
            )
        )
    freshest: dict[tuple[str, str, str, str], Any] = {}
    for row in rows:
        freshest[(row["event_key"], row["venue"], row["book"], row["outcome"])] = row
    by_event: dict[str, list[Any]] = defaultdict(list)
    for row in freshest.values():
        by_event[row["event_key"]].append(row)
    return {
        event_key: legs
        for event_key, legs in by_event.items()
        if len({leg["venue"] for leg in legs}) >= 2
    }


def requote_event(legs: list[Any], fetchers: dict[str, Fetcher], kalshi_fee_rate: Decimal) -> list[Quote]:
    """Fresh, near-simultaneous quotes for one event's stored legs."""
    fetched_at = datetime.now(timezone.utc)
    quotes: list[Quote] = []
    for leg in legs:
        fetcher = fetchers.get(str(leg["venue"]))
        if fetcher is None:
            continue
        current = fetcher(leg)
        if current is None:
            continue
        price, depth = current
        fee = kalshi_fee_cost(price, kalshi_fee_rate) if leg["venue"] == "kalshi" else Decimal(leg["fee_cost"])
        quotes.append(
            Quote(
                event_key=leg["event_key"],
                venue=leg["venue"],
                book=leg["book"],
                market_type=leg["market_type"],
                line=None if leg["line"] is None else Decimal(leg["line"]),
                outcome_label=leg["outcome"],
                raw_price=price,
                cost=price,
                fee_cost=fee,
                depth_usd=depth,
                live=False,
                fetched_at=fetched_at,
            )
        )
    return quotes


def run_focus_pass(store: Store, config, fcfg: dict[str, Any],
                   fetchers: dict[str, Fetcher] | None = None,
                   now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    fetchers = fetchers if fetchers is not None else live_fetchers()
    bet_map = build_bet_map(store, now, int(fcfg["map_max_age_mins"]), list(fcfg["sports"]))
    if not bet_map:
        return 0, 0
    pause = float(fcfg["rotation_pause_secs"])
    quotes: list[Quote] = []
    for index, legs in enumerate(bet_map.values()):
        quotes.extend(requote_event(legs, fetchers, config.kalshi_fee_rate))
        if pause and index < len(bet_map) - 1:
            time.sleep(pause)
    detectable = [q for q in quotes if q.depth_usd is None or q.depth_usd >= config.min_executable_usd]
    opportunities = detect_true_arbs(
        detectable,
        bankroll=config.bankroll_usd,
        max_stake_pct=config.max_stake_pct,
        trusted_pairs=config.trusted_pairs,
        stale_warn=config.arb_stale_warn,
        max_leg_skew_secs=config.max_leg_skew_secs,
        max_start_drift_mins=config.max_start_drift_mins,
        match_suspect_prob_diff=config.match_suspect_prob_diff,
    )
    opportunities.extend(
        detect_divergences(
            detectable, config.divergence_threshold, config.trusted_pairs,
            config.max_leg_skew_secs, config.max_start_drift_mins, config.match_suspect_prob_diff,
        )
    )
    opportunities, suspects = split_match_suspects(opportunities)
    poll_id = store.start_poll("focus")
    store.save_poll_quotes(poll_id, quotes)
    store.save_poll_opportunities(poll_id, opportunities)
    store.save_suspect_matches(poll_id, suspects)
    _record_focus(store, poll_id, opportunities)
    store.finish_poll(poll_id, None, len(quotes), len(opportunities))
    return len(bet_map), len(opportunities)


def _record_focus(store: Store, poll_id: int, opportunities: list[Opportunity]) -> None:
    with store.connect() as con:
        active = _active_rows(con)
        for opp in opportunities:
            row = active.get(opp.fingerprint)
            if row is None:
                _insert_tracker_row(con, poll_id, opp, "OPEN", None, "focus-simultaneous")
            elif row["status"] == "OPEN" and int(row["opened_poll_id"]) != poll_id:
                # The daemon flagged it; focus re-found it at simultaneous prices.
                _insert_tracker_row(con, poll_id, opp, "CONFIRMED", int(row["id"]), "focus-confirmed-simultaneous")


def live_fetchers() -> dict[str, Fetcher]:
    orderbook_cache: dict[str, dict] = {}
    last_kalshi_call = [0.0]

    def kalshi(leg: dict[str, Any]) -> tuple[Decimal, Decimal | None] | None:
        book_id = str(leg["book"])
        if book_id not in orderbook_cache:
            wait = last_kalshi_call[0] + KALSHI_THROTTLE_SECS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last_kalshi_call[0] = time.monotonic()
            response = requests.get(f"{KALSHI_URL}/markets/{book_id}/orderbook", timeout=30, headers=HEADERS)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            orderbook_cache[book_id] = response.json()
        book = orderbook_cache[book_id].get("orderbook_fp") or orderbook_cache[book_id].get("orderbook") or {}
        side = "no" if (leg["market_type"] == "TOTAL" and leg["outcome"] == "UNDER") else "yes"
        opposing = book.get("yes_dollars" if side == "no" else "no_dollars") or []
        if not opposing:
            return None
        price = Decimal("1") - max(Decimal(str(level[0])) for level in opposing)
        if not Decimal("0") < price < Decimal("1"):
            return None
        return q6(price), _depth_at_ask(orderbook_cache[book_id], side, price)

    def polymarket(leg: dict[str, Any]) -> tuple[Decimal, Decimal | None] | None:
        response = requests.get(f"{CLOB_URL}/book", params={"token_id": leg["book"]}, timeout=30, headers=HEADERS)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        best = _best_ask(response.json().get("asks", []))
        return None if best is None else (q6(best[0]), best[1])

    return {"kalshi": kalshi, "polymarket": polymarket}
