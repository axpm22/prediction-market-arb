"""Kalshi mutually-exclusive event baskets (negrisk.kalshi_events — declared
in config since v3.5, implemented 2026-07-14 to close the venue imbalance:
three lanes scanned Polymarket wholesale while Kalshi's non-sports surface
was only touched via 9 mapped pairs).

Discovery: the events listing (verified 2026-07-14, data/verified_facts.md)
— `GET /events?status=open&with_nested_markets=true` carries the
`mutually_exclusive` flag (exhaustiveness proof condition a), `category`,
and nested markets directly, so basket scanning costs the pagination alone.
(First attempt used the global /markets listing; its first 2,000 rows were
99.8% sports parlays — recorded and replaced same day.) Every observed
two-sided book persists (rule 7) whether or not a basket flags."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from arb.logutil import get_logger
from arb.negrisk import quote_rows, scan_group
from arb.venues.base import Deadline, get_json
from arb.venues.kalshi import BASE

log = get_logger("arb.kalshi_events", "logs/scan.log")

_CAT_FEE = {"Politics": "politics", "Economics": "econ",
            "Climate and Weather": "weather", "World": "geopolitics"}


def _open_events(cfg, deadline) -> list[dict]:
    events, cursor, pages = [], "", 0
    cap = int(cfg["negrisk"]["scan_pages"])
    while pages < cap:
        if deadline.expired():
            log.warning("kalshi events discovery truncated by deadline at page %s",
                        pages)
            break
        url = f"{BASE}/events?status=open&limit=200&with_nested_markets=true"
        if cursor:
            url += f"&cursor={cursor}"
        data = get_json(url, cfg, deadline=deadline)
        events.extend(data.get("events", []))
        cursor = data.get("cursor") or ""
        pages += 1
        if not cursor:
            break
    else:
        if cursor:
            log.warning("kalshi events pagination cap hit; coverage truncated")
    return events


def _dollars(m, key) -> Decimal | None:
    v = m.get(key)
    if v is None:
        return None
    d = Decimal(str(v))
    return d if 0 < d < 1 else None  # sentinel 0/1 prices are not quotes


def _outcomes(markets: list[dict]) -> list[dict]:
    """Permissive: one-sided outcomes keep their observed sides (rule 7 —
    persistence never depends on scannability; the exhaustiveness proof
    handles one-sidedness downstream with an unproven verdict, never a crash)."""
    outs = []
    for m in markets:
        ya = _dollars(m, "yes_ask_dollars")
        size = m.get("yes_ask_size_fp")
        outs.append({"label": m.get("yes_sub_title") or m.get("subtitle")
                     or m.get("ticker", ""),
                     "yes_ask": ya, "yes_bid": _dollars(m, "yes_bid_dollars"),
                     "no_ask": _dollars(m, "no_ask_dollars"),
                     "no_bid": _dollars(m, "no_bid_dollars"),
                     "depth_usd": Decimal(str(size)) * ya
                     if size is not None and ya is not None else None,
                     "volume_24h": Decimal(str(m["volume_24h_fp"]))
                     if m.get("volume_24h_fp") is not None else None})
    return outs


def scan_kalshi_events(cfg, now: datetime) -> tuple[list[dict], list[dict], int]:
    """-> (quote_rows, baskets, n_groups_scanned)."""
    deadline = Deadline(cfg["execution"]["venue_deadline_secs"])
    wanted = {str(c) for c in cfg["nonsports"]["kalshi_categories"]}
    cands = []
    for e in _open_events(cfg, deadline):
        markets = e.get("markets") or []
        if e.get("category") in wanted \
                and 3 <= len(markets) <= int(cfg["negrisk"]["max_outcomes"]):
            cands.append(e)
    # largest groups first — where exhaustive baskets live; the cap bounds
    # quote volume per poll, not API calls (markets ride the events pages)
    cands.sort(key=lambda e: -len(e["markets"]))
    cap = int(cfg["negrisk"]["kalshi_event_checks"])
    quotes, baskets, scanned = [], [], 0
    for i, e in enumerate(cands):
        outs = _outcomes(e["markets"])
        et = e.get("event_ticker", "")
        cat = _CAT_FEE.get(str(e.get("category")), "politics")
        event_key = f"nonsports:kalshi:{et}"
        # rule 7: every observed book persists, capped or not, scannable or not
        quotes.extend(quote_rows(cfg, "kalshi", cat, event_key, outs, now))
        if i >= cap:
            continue  # cap bounds basket-scan volume, never persistence
        bs = scan_group(cfg, "kalshi", cat, event_key, e.get("title", et), outs,
                        venue_flag=bool(e.get("mutually_exclusive")))
        exp = min((m.get("expected_expiration_time") or m.get("close_time") or ""
                   for m in e["markets"]), default="") or None
        for b in bs:
            b["resolution_date"] = exp  # D2 horizon gate input
        baskets.extend(bs)
        scanned += 1
    return quotes, baskets, scanned
