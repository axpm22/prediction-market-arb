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


def fetch_negrisk_events(limit: int = 200, pages: int = 3) -> list[dict]:
    """Lever C: widen coverage beyond the volume-24h top — paginate so briefly-mispriced
    low-volume baskets (the ones worth catching) are seen too."""
    seen: dict[str, dict] = {}
    per_page = max(1, limit // max(1, pages))
    for page in range(pages):
        r = requests.get(f"{GAMMA}/events",
                         params={"closed": "false", "limit": per_page, "offset": page * per_page,
                                 "order": "volume24hr", "ascending": "false"},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for e in batch:
            if e.get("negRisk") and len(e.get("markets", [])) >= 2:
                seen[str(e.get("id", e.get("slug", id(e))))] = e
    return list(seen.values())


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
    fetcher = fetcher or (lambda: fetch_negrisk_events(config.negrisk_scan_limit, config.negrisk_scan_pages))
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
            config.negrisk_min_executable_usd,  # Lever C: lower single-settlement floor
            config.taker_price_buffer,
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


def lifetime_distribution(store: Store) -> dict[str, Any]:
    """Lever C deliverable: how fast NEGRISK baskets close. For each opened NEGRISK flag,
    the seconds until its first CLOSE/SETTLED child — the flag-lifetime distribution is itself
    the result (a basket that closes in seconds is uncapturable in practice)."""
    from datetime import datetime as _dt
    with store.connect() as con:
        opens = list(con.execute(
            "SELECT id, placed_at FROM flat_bets WHERE kind='NEGRISK' AND status='OPEN' ORDER BY id"))
        closes = list(con.execute(
            """SELECT reappearance_of, placed_at FROM flat_bets
                WHERE kind='NEGRISK' AND status IN ('CLOSED','SETTLED') AND reappearance_of IS NOT NULL"""))
    close_at: dict[int, str] = {}
    for row in closes:
        rid = int(row["reappearance_of"])
        if rid not in close_at or str(row["placed_at"]) < close_at[rid]:
            close_at[rid] = str(row["placed_at"])
    lifetimes: list[float] = []
    for row in opens:
        c = close_at.get(int(row["id"]))
        if not c:
            continue
        try:
            secs = (_dt.fromisoformat(c) - _dt.fromisoformat(str(row["placed_at"]))).total_seconds()
        except ValueError:
            continue
        if secs >= 0:
            lifetimes.append(secs)
    lifetimes.sort()
    n = len(lifetimes)

    def pct(p: float) -> float | None:
        return round(lifetimes[min(n - 1, int(p * n))], 1) if n else None

    return {"opened": len(opens), "closed": n,
            "p50_secs": pct(0.50), "p90_secs": pct(0.90), "max_secs": (lifetimes[-1] if n else None)}


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
