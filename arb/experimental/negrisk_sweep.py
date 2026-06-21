"""Live negative-risk basket sweep — S1. Discovers Polymarket negRisk events, fetches
real CLOB books per outcome, detects convert/overshoot arbs, records kind='NEGRISK'.

Single-venue + same resolution, so once exhaustive+exclusive is confirmed from venue
metadata these are trusted=1. Fixtures-first verified; this is the live path.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import requests

from arb.negrisk import NegRiskArb, find_negrisk_arb
from arb.pricing import money, q6
from arb.store import Store

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HEADERS = {"User-Agent": "arb-paper-scanner/0.1 (public read-only)"}


def fetch_negrisk_events(limit: int = 60) -> list[dict]:
    r = requests.get(f"{GAMMA}/events", params={"closed": "false", "limit": limit,
                     "order": "volume24hr", "ascending": "false"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [e for e in r.json() if e.get("negRisk") and len(e.get("markets", [])) >= 2]


def attach_books(event: dict) -> dict:
    """Fill yes_best_ask / yes_best_bid / yes_ask_depth per market from the real CLOB."""
    for m in event.get("markets", []):
        tokens = m.get("clobTokenIds")
        tokens = json.loads(tokens) if isinstance(tokens, str) else (tokens or [])
        if not tokens:
            m["yes_best_ask"] = m["yes_best_bid"] = ""
            continue
        resp = requests.get(f"{CLOB}/book", params={"token_id": tokens[0]}, headers=HEADERS, timeout=30)
        if resp.status_code == 404:
            m["yes_best_ask"] = m["yes_best_bid"] = ""
            m["yes_ask_depth"] = m["yes_bid_depth"] = "0"
            continue
        resp.raise_for_status()
        book = resp.json()
        asks = _levels(book.get("asks", []))
        bids = _levels(book.get("bids", []))
        if asks:
            p, s = min(asks)
            m["yes_best_ask"], m["yes_ask_depth"] = str(p), str(q6(p * s))
        else:
            m["yes_best_ask"], m["yes_ask_depth"] = "", "0"
        if bids:
            p, s = max(bids)
            m["yes_best_bid"], m["yes_bid_depth"] = str(p), str(q6(p * s))
        else:
            m["yes_best_bid"], m["yes_bid_depth"] = "", "0"
    return event


def _levels(raw_levels: list[dict[str, Any]]) -> list[tuple[Decimal, Decimal]]:
    levels: list[tuple[Decimal, Decimal]] = []
    for level in raw_levels:
        price = Decimal(str(level["price"]))
        size = Decimal(str(level["size"]))
        if price > 0 and size > 0:
            levels.append((price, size))
    return levels


def run_negrisk_pass(store: Store, config, fetcher: Callable[[], list[dict]] | None = None,
                     book_filler: Callable[[dict], dict] | None = None) -> tuple[int, int]:
    fetcher = fetcher or fetch_negrisk_events
    book_filler = book_filler or attach_books
    events = fetcher()
    found: list[NegRiskArb] = []
    for event in events:
        arb = find_negrisk_arb(
            book_filler(event),
            config.polymarket_fee_rate,
            config.negrisk_min_edge,
            config.negrisk_require_exhaustive,
            config.negrisk_max_outcomes,
            config.min_executable_usd,
        )
        if arb is not None:
            found.append(arb)
    poll_id = store.start_poll("negrisk")
    _persist(store, poll_id, found)
    store.finish_poll(poll_id, None, len(events), len(found))
    return len(events), len(found)


def _persist(store: Store, poll_id: int, arbs: list[NegRiskArb]) -> None:
    with store.connect() as con:
        for a in arbs:
            fp = f"negrisk:{a.event_id}:{a.kind}"
            if con.execute("SELECT 1 FROM flat_bets WHERE fingerprint=? AND status='OPEN' LIMIT 1", (fp,)).fetchone():
                continue
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, status, placed_at,
                       notes, quality_flag, basket_n, basket_sum)
                   VALUES (?, ?, 'nonsports', 'negrisk', 'NEGRISK', ?, 0, ?, '1.00', ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)""",
                (fp, poll_id, int(a.exhaustive_exclusive), str(a.edge), json.dumps(a.legs),
                 str(a.basket_sum), str(money(a.edge)),
                 datetime.now(timezone.utc).isoformat(),
                 _notes(a),
                 a.quality_flag,
                 a.basket_n, str(a.basket_sum)),
            )


def _notes(arb: NegRiskArb) -> str:
    parts = [
        f"{arb.kind} basket n={arb.basket_n}",
        f"sum={arb.basket_sum}",
        f"capturable=${arb.capturable_usd}",
    ]
    if arb.quality_flag:
        parts.append(arb.quality_flag)
    parts.append(arb.title[:50])
    return " | ".join(parts)
