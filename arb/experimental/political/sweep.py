"""Political sweep loop: fetch open markets for cached series, detect intra-market arbs,
persist every market scanned (research data) and every arb to the append-only ledger.

Cross-venue political arbitrage stays manual-map-only (see arb.experimental/nonsports.py): the
settlement-wording risk is the whole danger. This sweep only does the risk-free
intra-market play, so its flags are recorded trusted=1 (one rulebook, one market).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import requests

from arb.store import Store

from arb.experimental.political.intramarket import IntraMarketArb, find_intramarket_arb
from arb.experimental.political.series import load_series

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2"
HEADERS = {"User-Agent": "arb-paper-scanner/0.1 (public read-only)"}
THROTTLE_SECS = 0.15
MAX_RETRIES_429 = 4  # Kalshi shares a per-IP limit across all our processes; back off.

Fetcher = Callable[[str], list[dict[str, Any]]]


def run_political_pass(store: Store, config, pcfg: dict[str, Any], fetcher: Fetcher | None = None) -> tuple[int, int]:
    fetcher = fetcher or _live_fetcher()
    series = load_series(list(pcfg["categories"]))
    scanned = 0
    arbs: list[IntraMarketArb] = []
    for entry in series:
        try:
            markets = fetcher(entry["ticker"])
        except requests.RequestException as exc:
            print(f"political: {entry['ticker']} fetch failed: {exc}")
            continue
        scanned += len(markets)
        for market in markets:
            arb = find_intramarket_arb(market, config.kalshi_fee_rate, str(entry["category"]))
            if arb is not None:
                arbs.append(arb)
    poll_id = store.start_poll("political")
    _persist(store, poll_id, arbs, scanned)
    store.finish_poll(poll_id, None, scanned, len(arbs))
    return scanned, len(arbs)


def _persist(store: Store, poll_id: int, arbs: list[IntraMarketArb], scanned: int) -> None:
    with store.connect() as con:
        for arb in arbs:
            legs = [
                {"venue": "kalshi", "book": arb.ticker, "outcome_label": "YES",
                 "cost": str(arb.yes_ask), "fee_cost": str(arb.yes_fee)},
                {"venue": "kalshi", "book": arb.ticker, "outcome_label": "NO",
                 "cost": str(arb.no_ask), "fee_cost": str(arb.no_fee)},
            ]
            fingerprint = f"intramarket:{arb.ticker}"
            existing = con.execute(
                "SELECT 1 FROM flat_bets WHERE fingerprint = ? AND status IN ('OPEN','CONFIRMED') LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                continue  # still active — not a new bet
            con.execute(
                """
                INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind,
                                      trusted, live, edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic,
                                      status, placed_at, notes)
                VALUES (?, NULL, ?, ?, 'political', 'ARB', 1, 0, ?, '1.00', ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    fingerprint, poll_id, _category_slug(arb.category), str(arb.roi),
                    json.dumps(legs), str(arb.total_cost),
                    str((Decimal("1") / arb.total_cost - Decimal("1")).quantize(Decimal("0.01"))),
                    datetime.now(timezone.utc).isoformat(),
                    f"intra-market YES+NO={arb.total_cost} | {arb.title[:60]}",
                ),
            )


def _category_slug(category: str) -> str:
    return category.lower().split()[0]  # "politics" / "economics" / "climate"


def _live_fetcher() -> Fetcher:
    last_call = [0.0]

    def fetch(series_ticker: str) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor = ""
        while True:
            url = f"{KALSHI_URL}/markets?series_ticker={series_ticker}&status=open&limit=200"
            if cursor:
                url += f"&cursor={cursor}"
            data = _get_with_backoff(url, last_call)
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor") or ""
            if not cursor:
                return markets

    return fetch


def _get_with_backoff(url: str, last_call: list[float]) -> dict[str, Any]:
    """Throttled GET that retries 429 (shared Kalshi per-IP limit) instead of dropping the
    series — the old code raised on the first 429, silently losing that series' data."""
    for attempt in range(MAX_RETRIES_429 + 1):
        wait = last_call[0] + THROTTLE_SECS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last_call[0] = time.monotonic()
        response = requests.get(url, timeout=30, headers=HEADERS)
        if response.status_code == 429 and attempt < MAX_RETRIES_429:
            time.sleep(float(response.headers.get("Retry-After") or 2 ** attempt))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Kalshi kept returning 429 for {url}")
