"""Non-sports markets (politics/econ/weather) — SPEC §1/§5.4, separate process.

Two matching paths (spec amendment, prompts/nonsports_ai_matcher.md):
  1. Manual (always on): `nonsports-suggest` fuzzy-pairs venue titles into
     logs/nonsports_candidates.log; a human reads both rulebooks and promotes a pair into
     data/nonsports_map.yaml. Human entries always win over an AI verdict.
  2. AI (opt-in via ai_matcher.enabled): Haiku compares both venues' resolution rules and
     decides equivalence inline; matched events are tagged ai_matched, always REVIEW, and
     report in their own section — the ledger measures Haiku's error rate, never trusts it.
Every flag is REVIEW; settlement is manual.
"""
from __future__ import annotations

import json
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
from arb.pricing import all_in_fee_cost
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


def fetch_mapped_quotes(
    entries: list[dict[str, Any]],
    kalshi_fee_rate: Decimal,
    polymarket_fee_rate: Decimal = Decimal("0"),
    taker_buffer: Decimal = Decimal("0"),
) -> list[Quote]:
    fetched_at = datetime.now(timezone.utc)
    quotes: list[Quote] = []
    for entry in entries:
        key = f"nonsports:{entry['id']}"
        close_by = entry["close_by"] if isinstance(entry["close_by"], date) else date.fromisoformat(str(entry["close_by"]))
        start = datetime(close_by.year, close_by.month, close_by.day, tzinfo=timezone.utc)
        quotes.extend(_kalshi_entry_quotes(entry, key, start, fetched_at, kalshi_fee_rate, taker_buffer))
        quotes.extend(_polymarket_entry_quotes(entry, key, start, fetched_at, polymarket_fee_rate, taker_buffer))
    return quotes


def paper_booked(opportunities: list[Opportunity], paper_fill: bool) -> list[Opportunity]:
    """Which opportunities get booked into the paper ledger. AI-matched flags book only when
    paper_fill is on (segregated evidence lane); manual map entries always book."""
    return [o for o in opportunities if paper_fill or not o.event.ai_matched]


def detect_nonsports(quotes: list[Quote], config,
                     ai_confidence: dict[str, str] | None = None,
                     ai_pairkeys: dict[str, str] | None = None) -> list[Opportunity]:
    detected = detect_true_arbs(
        quotes,
        bankroll=config.bankroll_usd,
        max_stake_pct=config.max_stake_pct,
        trusted_pairs=set(),  # non-sports pairs are never trusted
        stale_warn=config.arb_stale_warn,
    )
    detected.extend(detect_divergences(quotes, config.divergence_threshold, set()))
    flagged = []
    for opp in detected:
        conf = (ai_confidence or {}).get(opp.event.event_key)
        notes = (*opp.notes, "nonsports")
        event = opp.event
        if conf is not None:
            notes = (*notes, f"ai-matched conf={conf}")
            pairkey = (ai_pairkeys or {}).get(opp.event.event_key)
            if pairkey:  # carried so settlement-mismatch can ban the exact pair (STRIKE_AWARE)
                notes = (*notes, f"pair={pairkey}")
            event = replace(event, ai_matched=True)
        flagged.append(replace(opp, category="nonsports", status="REVIEW", notes=notes, event=event))
    return flagged


def run_nonsports_once(store: Store, config, ncfg: dict[str, Any]) -> tuple[int, int]:
    entries = load_map(max_days=config.nonsports_max_days)
    # Latency rule: quote collection for known entries first, Haiku lookups after.
    quotes = fetch_mapped_quotes(entries, config.kalshi_fee_rate, config.polymarket_fee_rate, config.taker_price_buffer)
    ai_cfg = getattr(config, "ai_matcher", None) or {}
    ai_confidence: dict[str, str] = {}
    ai_pairkeys: dict[str, str] = {}
    if ai_cfg.get("enabled"):
        try:
            ai_entries = ai_matched_entries(store, ai_cfg, ncfg, entries,
                                            max_days=config.nonsports_max_days)
        except Exception as exc:  # noqa: BLE001 — matcher failures never kill the watch loop
            print(f"ai-matcher: stage failed this poll: {exc}")
            ai_entries = []
        for entry in ai_entries:
            event_key = f"nonsports:{entry['id']}"
            ai_confidence[event_key] = str(entry["ai_confidence"])
            if entry.get("pair_key"):
                ai_pairkeys[event_key] = str(entry["pair_key"])
        ai_quotes = fetch_mapped_quotes(ai_entries, config.kalshi_fee_rate,
                                        config.polymarket_fee_rate, config.taker_price_buffer)
        quotes.extend(replace(q, notes=(*q.notes, "ai-matched")) for q in ai_quotes)
    else:
        print("ai-matcher: disabled — non-sports matching stays manual-only.")
    if not quotes:
        return 0, 0
    opportunities = detect_nonsports(quotes, config, ai_confidence, ai_pairkeys)
    poll_id = store.start_poll("nonsports")
    _save_nonsports_quotes(store, poll_id, quotes)
    store.save_poll_opportunities(poll_id, opportunities)
    # paper_fill gate: AI-matched flags book into the paper ledger only when enabled
    # (evidence factory). Off => display-only (saved as opportunities, not booked). Manual
    # map entries always book. record_tracker_poll dedups a persisting opp by position_key.
    booked = paper_booked(opportunities, bool(ai_cfg.get("paper_fill")))
    record_tracker_poll(store, poll_id, booked)
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


def fetch_polymarket_events(tags: list[str]) -> list[dict[str, Any]]:
    """Full Gamma event payloads (title, description, markets with clobTokenIds) — the AI
    matcher reuses the discovery payload's description as rules text (never re-fetched)."""
    events: list[dict[str, Any]] = []
    for tag in tags:
        events.extend(_get(f"{GAMMA_URL}/events?tag_slug={tag}&closed=false&limit=100"))
    return events


def ai_matched_entries(store: Store, ai_cfg: dict[str, Any], ncfg: dict[str, Any],
                       human_entries: list[dict[str, Any]],
                       max_days: int | None = None,
                       kalshi_titles: list[tuple[str, str]] | None = None,
                       polymarket_events: list[dict[str, Any]] | None = None,
                       client_factory=None) -> list[dict[str, Any]]:
    """Map-style entries for AI-matched pairs. Fuzzy candidates at the discovery level
    (kalshi series x polymarket event) split by structure: a single open market on each
    venue keeps the one-shot path (political); multi-market series (weather/macro) go
    through the deterministic strike matcher (STRIKE_AWARE) so only strike-aligned pairs
    reach Haiku. Human map entries always win; verdicts cache at market level."""
    from arb.nonsports_ai import MatcherUnavailable, make_client

    now = datetime.now(timezone.utc)
    client_factory = client_factory or make_client
    kalshi_titles = kalshi_titles if kalshi_titles is not None else fetch_kalshi_series_titles(list(ncfg["kalshi_categories"]))
    polymarket_events = polymarket_events if polymarket_events is not None else fetch_polymarket_events(list(ncfg["polymarket_tags"]))
    events_by_id = {str(e["id"]): e for e in polymarket_events}
    candidates = suggest_candidates(
        kalshi_titles, [(str(e["id"]), str(e["title"])) for e in polymarket_events],
        int(ncfg["candidate_threshold"]),
    )
    human_series = {str(entry.get("kalshi_series")) for entry in human_entries}
    entries: list[dict[str, Any]] = []
    for _score, series_ticker, k_title, pm_id, pm_title in candidates:
        if series_ticker in human_series:
            continue  # human file wins over any AI verdict
        pm_event = events_by_id.get(pm_id)
        if pm_event is None:
            continue
        try:
            k_markets = _kalshi_open_markets(series_ticker)
        except requests.RequestException as exc:
            print(f"ai-matcher: skip kalshi:{series_ticker} — detail fetch failed: {exc}")
            continue  # one venue hiccup must not kill the whole stage
        pm_markets = pm_event.get("markets") or []
        try:
            if len(k_markets) == 1 and len(pm_markets) == 1:
                entry = _single_market_entry(store, ai_cfg, k_markets[0], series_ticker,
                                             k_title, pm_id, pm_title, pm_event, now, client_factory)
                if entry is not None:
                    entries.append(entry)
            else:
                entries.extend(_strike_stage_entries(store, ai_cfg, ncfg, series_ticker,
                                                     k_markets, pm_markets, k_title, now, client_factory))
        except MatcherUnavailable as exc:
            # Billing/auth: stop asking this poll, keep whatever already matched.
            print(f"ai-matcher: unavailable, deferring new pairs: {exc}")
            break
    # Same near-future window as the human map.
    return load_map_entries(entries, max_days)


def _single_market_entry(store, ai_cfg, market, series_ticker, k_title, pm_id, pm_title,
                         pm_event, now, client_factory) -> dict[str, Any] | None:
    """One-shot (single market per venue) path. Market-level cache key with a legacy
    series-level fallback so the pre-STRIKE verdicts (keyed by series) still hit."""
    from arb.nonsports_ai import cached_verdict, pair_key, resolve_verdict, verdict_matches

    tokens = _polymarket_single_market_tokens(pm_event)
    if market is None or tokens is None:
        return None
    key = pair_key("kalshi", str(market["ticker"]), "polymarket", pm_id)
    legacy = pair_key("kalshi", series_ticker, "polymarket", pm_id)
    row = cached_verdict(store, key) or cached_verdict(store, legacy)
    if row is not None and not verdict_matches(row, ai_cfg["min_confidence"]):
        return None
    if row is None:
        row = resolve_verdict(
            store, ai_cfg,
            {"venue": "kalshi", "id": str(market["ticker"]), "title": k_title, "rules": _kalshi_rules(market)},
            {"venue": "polymarket", "id": pm_id, "title": pm_title, "rules": str(pm_event.get("description") or "")},
            now, client_factory,
        )
    if not verdict_matches(row, ai_cfg["min_confidence"]):
        return None
    return _ai_entry(market, pm_id, tokens, row, key, series_ticker)


def _strike_stage_entries(store, ai_cfg, ncfg, series_ticker, k_markets, pm_markets,
                          k_title_fallback, now, client_factory) -> list[dict[str, Any]]:
    """STRIKE_AWARE stage: deterministic strike alignment first (free), Haiku only on
    strike-aligned survivors, verdicts cached at market level."""
    from arb.experimental.strike_match import strike_candidates
    from arb.nonsports_ai import cached_verdict, pair_key, resolve_verdict, verdict_matches

    gaps: list[str] = []
    cands = strike_candidates(k_markets, pm_markets, int(ncfg["candidate_threshold"]), on_parse_gap=gaps.append)
    for gap in gaps:
        print(f"ai-matcher: {gap}")
    k_by_ticker = {str(m.get("ticker")): m for m in k_markets}
    pm_by_cid = {str(m.get("conditionId") or m.get("id")): m for m in pm_markets}
    entries: list[dict[str, Any]] = []
    for cand in cands:
        key = pair_key("kalshi", cand.kalshi_ticker, "polymarket", cand.polymarket_condition_id)
        row = cached_verdict(store, key)
        if row is not None and not verdict_matches(row, ai_cfg["min_confidence"]):
            continue  # negative verdict short-circuits stage 2 forever
        kmkt = k_by_ticker.get(cand.kalshi_ticker)
        pmkt = pm_by_cid.get(cand.polymarket_condition_id)
        tokens = _pm_market_tokens(pmkt) if pmkt else None
        if kmkt is None or tokens is None:
            continue
        if row is None:
            row = resolve_verdict(
                store, ai_cfg,
                {"venue": "kalshi", "id": cand.kalshi_ticker,
                 "title": str(kmkt.get("title") or k_title_fallback), "rules": _kalshi_rules(kmkt)},
                {"venue": "polymarket", "id": cand.polymarket_condition_id,
                 "title": str(pmkt.get("question") or ""), "rules": str(pmkt.get("description") or "")},
                now, client_factory,
            )
        if not verdict_matches(row, ai_cfg["min_confidence"]):
            continue
        entries.append(_ai_entry(kmkt, cand.polymarket_condition_id, tokens, row, key, series_ticker))
    return entries


def _ai_entry(market, pm_id, tokens, row, key, series_ticker) -> dict[str, Any]:
    return {
        "id": f"ai:{market['ticker']}:{pm_id}", "enabled": True, "ai_matched": True,
        "ai_confidence": str(row["confidence"]), "pair_key": key,
        "close_by": str(market.get("close_time", ""))[:10],
        "kalshi_series": series_ticker, "kalshi_ticker": str(market["ticker"]),
        "polymarket_tokens": tokens,
    }


def load_map_entries(entries: list[dict[str, Any]], max_days: int | None) -> list[dict[str, Any]]:
    """Apply load_map's enabled/close_by window to in-memory entries (AI-matched ones)."""
    today = datetime.now(timezone.utc).date()
    kept = []
    for entry in entries:
        try:
            close_by = date.fromisoformat(str(entry["close_by"]))
        except ValueError:
            continue
        if close_by < today:
            continue
        if max_days is not None and close_by > today + timedelta(days=max_days):
            continue
        kept.append(entry)
    return kept


def _kalshi_open_markets(series_ticker: str) -> list[dict[str, Any]]:
    return _get_throttled(f"{KALSHI_URL}/markets?series_ticker={series_ticker}&status=open&limit=200").get("markets", [])


def _get_throttled(url: str) -> Any:
    """Kalshi politeness for the AI stage's per-candidate detail GETs: pace requests and
    back off once on 429 (the burst of candidate lookups otherwise trips the rate limit)."""
    import time

    time.sleep(0.2)
    response = requests.get(url, timeout=30, headers=HEADERS)
    if response.status_code == 429:
        time.sleep(2.0)
        response = requests.get(url, timeout=30, headers=HEADERS)
    response.raise_for_status()
    return response.json()


def _kalshi_rules(market: dict[str, Any]) -> str:
    rules = str(market.get("rules_primary") or "")
    if not rules:  # listing omitted rules — spec: GET /markets/{ticker}
        detail = _get(f"{KALSHI_URL}/markets/{market['ticker']}").get("market", {})
        rules = str(detail.get("rules_primary") or "")
        market.setdefault("rules_secondary", detail.get("rules_secondary"))
    secondary = str(market.get("rules_secondary") or "")
    return f"{rules}\n{secondary}".strip()


def _polymarket_single_market_tokens(event: dict[str, Any]) -> dict[str, str] | None:
    markets = event.get("markets") or []
    if len(markets) != 1:
        return None
    return _pm_market_tokens(markets[0])


def _pm_market_tokens(market: dict[str, Any] | None) -> dict[str, str] | None:
    """{YES: token_id, NO: token_id} for one Polymarket market (outcomes ↔ clobTokenIds)."""
    if not market:
        return None
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        token_ids = json.loads(market.get("clobTokenIds") or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if len(outcomes) != len(token_ids) or not token_ids:
        return None
    return {str(label).upper(): str(token) for label, token in zip(outcomes, token_ids)}


def _kalshi_entry_quotes(entry, key: str, start, fetched_at, fee_rate: Decimal, taker_buffer: Decimal) -> list[Quote]:
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
                fee_cost=all_in_fee_cost(price, fee_rate, taker_buffer), depth_usd=_depth_at_ask(book, side, price),
                live=False, fetched_at=fetched_at, start_utc=start, notes=("nonsports",),
            )
        )
    return quotes


def _polymarket_entry_quotes(entry, key: str, start, fetched_at, fee_rate: Decimal, taker_buffer: Decimal) -> list[Quote]:
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
                fee_cost=all_in_fee_cost(price, fee_rate, taker_buffer), depth_usd=depth, live=False, fetched_at=fetched_at,
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
