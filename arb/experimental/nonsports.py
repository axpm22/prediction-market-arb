"""Non-sports markets (politics/econ/weather) — SPEC §1/§5.4, separate process.

Hard rule: NEVER auto-match non-sports. The settlement wording IS the risk — two venues
can phrase near-identical questions that resolve differently. The flow is:
  1. `nonsports-suggest` fuzzy-pairs venue titles into logs/nonsports_candidates.log,
  2. a human reads both rulebooks and promotes a pair into data/nonsports_map.yaml,
  3. this watcher quotes only mapped entries; every flag is REVIEW; settlement is manual.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
import yaml

from arb.arbitrage import detect_divergences, detect_true_arbs
from arb.matching import normalize_name, token_ratio
from arb.models import Opportunity, Quote
from arb.pricing import kalshi_fee_cost
from arb.store import Store
from arb.tracker import record_tracker_poll
from arb.venues.kalshi import BASE_URL as KALSHI_URL
from arb.venues.kalshi import _depth_at_ask, _is_empty_book
from arb.venues.polymarket import CLOB_URL, GAMMA_URL, _best_ask

MAP_PATH = Path("data/nonsports_map.yaml")
CANDIDATES_LOG = Path("logs/nonsports_candidates.log")
HEADERS = {"User-Agent": "arb-paper-scanner/0.1 (public read-only)"}


def load_map(path: Path = MAP_PATH, today: date | None = None, max_days: int | None = None) -> list[dict[str, Any]]:
    entries = yaml.safe_load(path.read_text()) or []
    today = today or datetime.now(timezone.utc).date()
    kept = []
    for entry in entries:
        if not entry.get("enabled"):
            continue
        close_by = entry["close_by"] if isinstance(entry["close_by"], date) else date.fromisoformat(str(entry["close_by"]))
        if close_by < today:
            continue
        if max_days is not None and close_by > today + timedelta(days=max_days):
            continue
        kept.append(entry)
    return kept


def fetch_mapped_quotes(entries: list[dict[str, Any]], kalshi_fee_rate: Decimal) -> list[Quote]:
    fetched_at = datetime.now(timezone.utc)
    quotes: list[Quote] = []
    for entry in entries:
        key = f"nonsports:{entry['id']}"
        close_by = entry["close_by"] if isinstance(entry["close_by"], date) else date.fromisoformat(str(entry["close_by"]))
        start = datetime(close_by.year, close_by.month, close_by.day, tzinfo=timezone.utc)
        quotes.extend(_kalshi_entry_quotes(entry, key, start, fetched_at, kalshi_fee_rate))
        quotes.extend(_polymarket_entry_quotes(entry, key, start, fetched_at))
    return quotes


def detect_nonsports(quotes: list[Quote], config) -> list[Opportunity]:
    detected = detect_true_arbs(
        quotes,
        bankroll=config.bankroll_usd,
        max_stake_pct=config.max_stake_pct,
        trusted_pairs=set(),  # non-sports pairs are never trusted
        stale_warn=config.arb_stale_warn,
    )
    detected.extend(detect_divergences(quotes, config.divergence_threshold, set()))
    return [
        replace(opp, category="nonsports", status="REVIEW", notes=(*opp.notes, "nonsports"))
        for opp in detected
    ]


def run_nonsports_once(store: Store, config, ncfg: dict[str, Any]) -> tuple[int, int]:
    entries = load_map(max_days=config.nonsports_max_days)
    if not entries:
        return 0, 0
    quotes = fetch_mapped_quotes(entries, config.kalshi_fee_rate)
    opportunities = detect_nonsports(quotes, config)
    poll_id = store.start_poll("nonsports")
    _save_nonsports_quotes(store, poll_id, quotes)
    store.save_poll_opportunities(poll_id, opportunities)
    record_tracker_poll(store, poll_id, opportunities)
    store.finish_poll(poll_id, None, len(quotes), len(opportunities))
    return len(quotes), len(opportunities)


def suggest_candidates(
    kalshi_titles: list[tuple[str, str]],
    polymarket_titles: list[tuple[str, str]],
    threshold: int,
) -> list[tuple[int, str, str, str, str]]:
    """Fuzzy title pairs (score, kalshi_ticker, kalshi_title, pm_id, pm_title) — suggestions only."""
    matches = []
    for k_ticker, k_title in kalshi_titles:
        for pm_id, pm_title in polymarket_titles:
            score = token_ratio(
                normalize_name(k_title, "nonsports", aliases={}),
                normalize_name(pm_title, "nonsports", aliases={}),
            )
            if score >= threshold:
                matches.append((score, k_ticker, k_title, pm_id, pm_title))
    return sorted(matches, reverse=True)


def write_candidates(matches: list[tuple[int, str, str, str, str]], log_path: Path = CANDIDATES_LOG) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    existing = set(log_path.read_text().splitlines()) if log_path.exists() else set()
    added = 0
    with log_path.open("a") as handle:
        for score, k_ticker, k_title, pm_id, pm_title in matches:
            line = f"{score} | kalshi:{k_ticker} | {k_title} | polymarket:{pm_id} | {pm_title}"
            if line in existing:
                continue
            handle.write(line + "\n")
            added += 1
    return added


def fetch_kalshi_series_titles(categories: list[str], max_pages: int = 12) -> list[tuple[str, str]]:
    titles: list[tuple[str, str]] = []
    for category in categories:
        cursor = ""
        for _ in range(max_pages):
            url = f"{KALSHI_URL}/series?category={requests.utils.quote(category)}&limit=200"
            if cursor:
                url += f"&cursor={cursor}"
            data = _get(url)
            titles.extend((str(s["ticker"]), str(s["title"])) for s in data.get("series", []))
            cursor = data.get("cursor") or ""
            if not cursor:
                break
    return titles


def fetch_polymarket_titles(tags: list[str]) -> list[tuple[str, str]]:
    titles: list[tuple[str, str]] = []
    for tag in tags:
        events = _get(f"{GAMMA_URL}/events?tag_slug={tag}&closed=false&limit=100")
        titles.extend((str(e["id"]), str(e["title"])) for e in events)
    return titles


def _kalshi_entry_quotes(entry, key: str, start, fetched_at, fee_rate: Decimal) -> list[Quote]:
    if not entry.get("kalshi_series") or not entry.get("kalshi_ticker"):
        return []
    data = _get(f"{KALSHI_URL}/markets?series_ticker={entry['kalshi_series']}&status=open&limit=200")
    market = next((m for m in data.get("markets", []) if m["ticker"] == entry["kalshi_ticker"]), None)
    if market is None or _is_empty_book(market):
        return []
    book = _get(f"{KALSHI_URL}/markets/{market['ticker']}/orderbook")
    quotes = []
    for label, side in [("YES", "yes"), ("NO", "no")]:
        price = Decimal(str(market[f"{side}_ask_dollars"]))
        if not Decimal("0") < price < Decimal("1"):
            continue
        quotes.append(
            Quote(
                event_key=key, venue="kalshi", book=str(market["ticker"]), market_type="WINNER",
                line=None, outcome_label=label, raw_price=price, cost=price,
                fee_cost=kalshi_fee_cost(price, fee_rate), depth_usd=_depth_at_ask(book, side, price),
                live=False, fetched_at=fetched_at, start_utc=start, notes=("nonsports",),
            )
        )
    return quotes


def _polymarket_entry_quotes(entry, key: str, start, fetched_at) -> list[Quote]:
    quotes = []
    for label, token_id in (entry.get("polymarket_tokens") or {}).items():
        response = requests.get(f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=30, headers=HEADERS)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        best = _best_ask(response.json().get("asks", []))
        if best is None:
            continue
        price, depth = best
        quotes.append(
            Quote(
                event_key=key, venue="polymarket", book=str(token_id), market_type="WINNER",
                line=None, outcome_label=str(label).upper(), raw_price=price, cost=price,
                fee_cost=Decimal("0"), depth_usd=depth, live=False, fetched_at=fetched_at,
                start_utc=start, notes=("nonsports",),
            )
        )
    return quotes


def _save_nonsports_quotes(store: Store, poll_id: int, quotes: list[Quote]) -> None:
    # arb.store hardcodes category='sports'; non-sports rows are written here instead.
    with store.connect() as con:
        con.executemany(
            """
            INSERT INTO quotes(poll_id, fetched_at, event_key, category, sport, venue, book,
                               market_type, line, outcome, raw_price, cost, fee_cost, depth_usd, live)
            VALUES (?, ?, ?, 'nonsports', 'nonsports', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            [
                (
                    poll_id, q.fetched_at.isoformat(), q.event_key, q.venue, q.book, q.market_type,
                    None if q.line is None else str(q.line), q.outcome_label, str(q.raw_price),
                    str(q.cost), str(q.fee_cost), None if q.depth_usd is None else str(q.depth_usd),
                )
                for q in quotes
            ],
        )


def _get(url: str) -> Any:
    response = requests.get(url, timeout=30, headers=HEADERS)
    response.raise_for_status()
    return response.json()
