"""LIVE lane (prompts/PROMPT_LIVE_LEADLAG.md Part A). In-play $1 paper flags,
review-lane forever by classify(); pre-registered kill gate in PREREGISTRATION.md.

THE ODDS API IS NEVER IMPORTED OR CALLED HERE (rule 2) — keyless venue
endpoints only. Markouts are pregame-valid (§10.5 v3.4): live bets take no
markout rows, and that is deliberate — do not "fix" it. Their honesty
instruments are the confirm rate, lifetime, and locked-vs-confirmed drift.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.arbitrage import detect
from arb.cycle import persist_poll
from arb.logutil import get_logger
from arb.matching import MatchedGroup
from arb.models import Event, Quote

log = get_logger("arb.live", "logs/scan.log")


def shortlist(cfg, store, now: datetime) -> list[dict]:
    """Events with start <= now < start + max_game_hours[sport], capped, ordered:
    events holding open live bets first, then most recent start (A2)."""
    rows = store.query(
        """SELECT event_key, sport, MAX(start_utc) start_utc,
                  MAX(venue_market_id IS NOT NULL) has_ids
           FROM quotes
           WHERE event_key != '' AND start_utc IS NOT NULL
             AND fetched_at > ?
           GROUP BY event_key""",
        # Python ISO bound, never datetime('now'): the 'T' separator makes the
        # SQLite form compare by date only (the invariants.py trap)
        ((now - timedelta(days=1)).isoformat(),))
    open_live = {r["event_key"] for r in store.query(
        """SELECT DISTINCT event_key FROM flat_bets fb
           WHERE fb.poll_kind = 'live'
             AND NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id = fb.id
                             AND be.kind IN ('lifetime_closed', 'void', 'settled'))""")}
    hours = cfg["live_lane"]["max_game_hours"]
    out = []
    for r in rows:
        cap = hours.get(r["sport"])
        if cap is None or not r["has_ids"]:
            continue
        start = datetime.fromisoformat(r["start_utc"])
        if start <= now < start + timedelta(hours=float(cap)):
            out.append({"event_key": r["event_key"], "sport": r["sport"],
                        "start_utc": start})
        # past the cap: leaves the shortlist; FINAL settles record the skip (A2)
    out.sort(key=lambda e: (e["event_key"] not in open_live,
                            -e["start_utc"].timestamp()))
    return out[:int(cfg["live_lane"]["shortlist_max_events"])]


def identities(store, event_keys: list[str]) -> list[dict]:
    """Latest stored identity per (event, venue, market, line, outcome, side) —
    the re-pricing target set."""
    if not event_keys:
        return []
    ph = ",".join("?" * len(event_keys))
    return [dict(r) for r in store.query(
        f"""SELECT event_key, sport, venue, market_type, line, outcome, side,
                   venue_market_id, MAX(start_utc) start_utc,
                   MAX(start_grade) start_grade
            FROM quotes
            WHERE event_key IN ({ph}) AND venue_market_id IS NOT NULL
              AND venue_market_id != ''
            GROUP BY event_key, venue, market_type, line, outcome, side""",
        event_keys)]


def _requote_kalshi(cfg, idents: list[dict], now) -> list[Quote]:
    from arb.venues.kalshi import SPORT_SERIES, parse_markets
    from arb.venues.base import get_json
    wanted = {i["venue_market_id"] for i in idents}
    series = {i["venue_market_id"].split("-")[0] for i in idents}
    quotes = []
    for sport, entries in SPORT_SERIES.items():
        for ticker, mtype in entries:
            if ticker not in series:
                continue
            data = get_json(
                "https://api.elections.kalshi.com/trade-api/v2/markets"
                f"?series_ticker={ticker}&status=open&limit=200", cfg)
            quotes.extend(q for q in parse_markets(
                cfg, data.get("markets", []), sport, mtype)
                if q.venue_market_id in wanted)
    return quotes


def _requote_polymarket(cfg, idents: list[dict], now) -> list[Quote]:
    from arb.venues.fees import leg_fee_cost
    from arb.venues.polymarket import fetch_books
    tokens = {i["venue_market_id"].split(":")[1]: i for i in idents
              if ":" in (i["venue_market_id"] or "")}
    books = fetch_books(cfg, list(tokens))
    quotes = []
    for token, ident in tokens.items():
        book = books.get(token)
        if not book or not book.get("asks"):
            continue
        asks = sorted(book["asks"], key=lambda x: Decimal(x["price"]))
        bids = sorted(book.get("bids", []), key=lambda x: Decimal(x["price"]),
                      reverse=True)
        ask = Decimal(asks[0]["price"])
        start = datetime.fromisoformat(ident["start_utc"]) if ident["start_utc"] else None
        quotes.append(Quote(
            event_key=ident["event_key"], venue="polymarket", book="polymarket",
            market_type=ident["market_type"], line=ident["line"],
            outcome_label=ident["outcome"], side=ident["side"],
            venue_side=ident["side"], raw_price=ask, cost=ask,
            fee_cost=leg_fee_cost(cfg, "polymarket", "sports", ask),
            depth_usd=Decimal(asks[0]["size"]) * ask,
            ladder_json=json.dumps([[a["price"], a["size"]] for a in asks]),
            live=True, fetched_at=datetime.now(timezone.utc), category="sports",
            sport=ident["sport"],
            bid=Decimal(bids[0]["price"]) if bids else None, volume_24h=None,
            start_utc=start, start_grade=ident["start_grade"] or "nominal",
            venue_market_id=ident["venue_market_id"]))
    return quotes


def _requote_sx(cfg, idents: list[dict], now) -> list[Quote]:
    from arb.venues.base import get_json
    from arb.venues.sx import quotes_from_orders
    hashes = sorted({i["venue_market_id"] for i in idents})
    if not hashes:
        return []
    markets = get_json("https://api.sx.bet/markets/find?marketHashes="
                       + ",".join(hashes), cfg).get("data", [])
    orders = get_json("https://api.sx.bet/orders?marketHashes="
                      + ",".join(hashes), cfg).get("data", [])
    sport_by_hash = {i["venue_market_id"]: i["sport"] for i in idents}
    key_by_hash = {i["venue_market_id"]: i["event_key"] for i in idents}
    quotes = []
    for m in markets:
        qs = quotes_from_orders(cfg, m, orders, sport_by_hash.get(m["marketHash"], ""))
        for q in qs:
            q.event_key = key_by_hash.get(m["marketHash"], "")
        quotes.extend(qs)
    return quotes


REQUOTERS = {"kalshi": _requote_kalshi, "polymarket": _requote_polymarket,
             "sx": _requote_sx}


def run_live_cycle(cfg, store, now: datetime | None = None,
                   dry_run: bool = False) -> dict:
    t0 = time.monotonic()
    now = now or datetime.now(timezone.utc)
    events = shortlist(cfg, store, now)
    idents = identities(store, [e["event_key"] for e in events])
    by_venue: dict[str, list[dict]] = {}
    for i in idents:
        by_venue.setdefault(i["venue"], []).append(i)

    quotes, status = [], {}
    for venue, vi in by_venue.items():
        t1 = time.monotonic()
        try:
            qs = REQUOTERS[venue](cfg, vi, now)
            quotes.extend(qs)
            status[venue] = {"ok": True, "n": len(qs),
                             "latency_ms": (time.monotonic() - t1) * 1000}
        except Exception as e:  # noqa: BLE001 — a venue outage is data
            log.warning("live requote failed %s: %s", venue, e)
            status[venue] = {"ok": False, "n": 0,
                             "latency_ms": (time.monotonic() - t1) * 1000,
                             "error": str(e)}

    ev_meta = {e["event_key"]: e for e in events}
    by_event: dict[str, list[Quote]] = {}
    for q in quotes:
        by_event.setdefault(q.event_key, []).append(q)
    groups = []
    for key, qs in by_event.items():
        meta = ev_meta.get(key)
        if not meta:
            continue
        names = key.split(":")
        groups.append(MatchedGroup(event=Event(
            event_key=key, category="sports", sport=meta["sport"],
            home=names[3] if len(names) > 3 else "", away=names[2] if len(names) > 2 else "",
            start_utc=meta["start_utc"], start_grade="nominal"), quotes=qs))

    opps, rejects = detect(cfg, groups, now=now, mode="live")
    coverage = len(quotes) / len(idents) if idents else 1.0
    elapsed = time.monotonic() - t0
    summary = {"shortlist": [e["event_key"] for e in events],
               "identities": len(idents), "requoted": len(quotes),
               "coverage": round(coverage, 4), "flags": len(opps),
               "rejects": len(rejects), "cycle_secs": round(elapsed, 2)}
    if dry_run:
        return summary
    poll_id = persist_poll(cfg, store, "live", quotes, status, groups, opps, rejects,
                           started=now)
    store.insert("annotations", {  # coverage per cycle, read by the daily report
        "created_at": now.isoformat(), "target_kind": "poll",
        "target_id": str(poll_id), "kind": "live-coverage",
        "detail": json.dumps(summary)})
    return summary
