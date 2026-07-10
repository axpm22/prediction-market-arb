"""Polymarket adapter (SPEC §6.2). Discovery via Gamma; EXECUTABLE PRICES FROM THE
CLOB ONLY — Gamma outcomePrices are stale mids, never used for detection (trap 2).
Batch books via POST /books (verified 2026-07-08). Full per-token ladders (bug B4).
MMA keys on tag_slug=ufc; canonical sport string stays mma."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

from arb.models import Quote
from arb.venues.base import Deadline, PartialFetch, get_json, is_live, post_json
from arb.venues.fees import leg_fee_cost

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SPORT_TAGS = {"baseball_mlb": "mlb", "basketball_nba": "nba", "soccer": "soccer",
              "tennis": "tennis", "mma": "ufc"}
IN_SCOPE_TYPES = {"moneyline": "WINNER", "totals": "TOTAL"}
BOOKS_CHUNK = 100


def _parse_start(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).astimezone(timezone.utc)
    except ValueError:
        return None


def fetch_gamma_events(cfg, tag: str, max_pages: int = 10, deadline=None) -> list[dict]:
    events, offset = [], 0
    for _ in range(max_pages):
        if deadline is not None and deadline.expired():
            break  # per-venue deadline (H7): stop paginating, return what we have
        page = get_json(f"{GAMMA}/events?tag_slug={tag}&closed=false&limit=100"
                        f"&offset={offset}", cfg)
        events.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return events


def fetch_books(cfg, token_ids: list[str], deadline=None) -> dict[str, dict]:
    books = {}
    for i in range(0, len(token_ids), BOOKS_CHUNK):
        if deadline is not None and deadline.expired():
            break  # per-venue deadline (H7)
        chunk = token_ids[i:i + BOOKS_CHUNK]
        for b in post_json(f"{CLOB}/books", cfg, [{"token_id": t} for t in chunk]):
            if b and b.get("asset_id"):
                books[b["asset_id"]] = b
    return books


def _strip_ufc_suffix(name: str) -> str:
    return name.split(" (")[0].strip()


def _teams_from_title(title: str) -> tuple[str, str]:
    parts = title.replace(" vs. ", " vs ").split(" vs ")
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", "")


def quotes_from_event(cfg, event: dict, books: dict[str, dict], sport: str) -> list[Quote]:
    quotes: list[Quote] = []
    now = datetime.now(timezone.utc)
    ev_away, ev_home = _teams_from_title(event.get("title", ""))
    for m in event.get("markets", []):
        mtype = IN_SCOPE_TYPES.get(m.get("sportsMarketType") or "")
        if mtype is None or not m.get("clobTokenIds"):
            continue  # method-of-victory / go-the-distance etc. out of scope
        outcomes = json.loads(m["outcomes"])
        tokens = json.loads(m["clobTokenIds"])
        if mtype == "WINNER" and len(outcomes) == 2:
            away, home = outcomes[0], outcomes[1]
        else:
            away, home = ev_away, ev_home
        start = _parse_start(m.get("gameStartTime"))
        line = str(Decimal(str(m["line"]))) if m.get("line") is not None else None
        vol = Decimal(str(m.get("volume24hr") or 0))
        for outcome, token in zip(outcomes, tokens):
            book = books.get(token)
            if not book:
                continue  # unknown depth is None -> non-actionable, never fetched=assumed
            asks = sorted(book.get("asks", []), key=lambda x: Decimal(x["price"]))
            bids = sorted(book.get("bids", []), key=lambda x: Decimal(x["price"]), reverse=True)
            if not asks:
                continue  # one-sided book: nothing executable to buy
            ask = Decimal(asks[0]["price"])
            label = outcome.upper() if mtype == "TOTAL" else _strip_ufc_suffix(outcome)
            quotes.append(Quote(
                event_key="", venue="polymarket", book="polymarket", market_type=mtype,
                line=line, outcome_label=label, side="YES", venue_side="YES", raw_price=ask, cost=ask,
                fee_cost=leg_fee_cost(cfg, "polymarket", "sports", ask),
                depth_usd=Decimal(asks[0]["size"]) * ask,
                ladder_json=json.dumps([[a["price"], a["size"]] for a in asks]),
                live=is_live(start, now), fetched_at=now, category="sports", sport=sport,
                bid=Decimal(bids[0]["price"]) if bids else None, volume_24h=vol,
                home=home, away=away, start_utc=start,
                start_grade="nominal" if start else "absent",
                venue_market_id=f"{m.get('conditionId', m.get('id'))}:{token}"))
    return quotes


def _fetch_sport(cfg, sport: str, deadline: Deadline) -> list[Quote]:
    quotes: list[Quote] = []
    tag = SPORT_TAGS.get(sport)
    if not tag or deadline.expired():
        return quotes
    events = fetch_gamma_events(cfg, tag, deadline=deadline)
    wanted = []
    for e in events:
        for m in e.get("markets", []):
            if m.get("sportsMarketType") in IN_SCOPE_TYPES and m.get("clobTokenIds"):
                wanted.extend(json.loads(m["clobTokenIds"]))
    books = fetch_books(cfg, wanted, deadline=deadline)
    for e in events:
        quotes.extend(quotes_from_event(cfg, e, books, sport))
    return quotes


def fetch_markets(cfg) -> list[Quote]:
    # per-sport-tag fan-out in parallel (H7): venue time ~ slowest single tag.
    deadline = Deadline(cfg["execution"]["venue_deadline_secs"])
    sports = list(cfg["sports"])
    with ThreadPoolExecutor(max_workers=max(1, len(sports))) as pool:
        per_sport = list(pool.map(lambda s: _fetch_sport(cfg, s, deadline), sports))
    quotes = [q for sub in per_sport for q in sub]
    if deadline.expired():
        raise PartialFetch(quotes)
    return quotes


def fetch_negrisk_events(cfg, tags: list[str]) -> list[dict]:
    """Events carrying the venue's structural negRisk flag (proof condition a).
    Deduped by event id: overlapping tags otherwise yield the same event twice,
    which process_poll would book as duplicate candidates (H10)."""
    seen, out = set(), []
    for tag in tags:
        for e in fetch_gamma_events(cfg, tag):
            if e.get("negRisk") and e.get("id") not in seen:
                seen.add(e.get("id"))
                e["_tag"] = tag  # source tag -> fee category (first tag wins)
                out.append(e)
    return out
