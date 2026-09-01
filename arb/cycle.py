"""One poll cycle: match -> detect -> persist (SPEC rule 7: every quote persists,
even when no opportunity is found). Tracker hook processes opportunities into
flat bets (Phase 6)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from rich.console import Console

from arb.arbitrage import detect
from arb.logutil import get_logger
from arb.matching import build_groups, load_aliases
from arb.models import Opportunity, position_key

log = get_logger("arb.cycle", "logs/scan.log")
console = Console()
NY = ZoneInfo("America/New_York")


def _quote_row(q, poll_id):
    return {
        "poll_id": poll_id, "fetched_at": q.fetched_at.isoformat(),
        "event_key": q.event_key, "category": q.category, "sport": q.sport,
        "venue": q.venue, "book": q.book, "market_type": q.market_type,
        "line": q.line, "outcome": q.outcome_label, "side": q.side,
        "raw_price": q.raw_price, "cost": q.cost, "fee_cost": q.fee_cost,
        "depth_usd": q.depth_usd, "ladder_json": q.ladder_json, "live": q.live,
        "bid": q.bid, "volume_24h": q.volume_24h,
        "start_utc": q.start_utc.isoformat() if q.start_utc else None,
        "start_grade": q.start_grade,
        "venue_market_id": q.venue_market_id,
    }


def _opp_row(o: Opportunity, poll_id):
    legs = [{"venue": q.venue, "book": q.book, "market_type": q.market_type,
             "line": q.line, "outcome": q.outcome_label, "side": q.side,
             "cost": str(q.cost), "fee_cost": str(q.fee_cost),
             # bid is what you can EXIT at, so it is what decides whether an
             # edge survives crossing. Absent until 2026-07-30, which made the
             # cover-survival funnel read every leg as unexitable and report
             # "no cover was ever executable" from a missing field rather than
             # from the market (same shape as the depth_usd gap before it).
             "bid": str(q.bid) if q.bid is not None else None,
             "depth_usd": str(q.depth_usd) if q.depth_usd is not None else None,
             "fetched_at": q.fetched_at.isoformat()} for q in o.legs]
    pk = position_key(o.kind, o.event.sport, o.event.event_key,
                      o.legs[0].market_type if o.legs else "WINNER",
                      o.legs[0].line if o.legs else None,
                      outcome=o.legs[0].outcome_label if o.legs else None)
    return {"poll_id": poll_id, "fingerprint": o.fingerprint, "position_key": pk,
            "kind": o.kind, "category": o.category, "event_key": o.event.event_key,
            "edge": o.edge, "status": o.status, "legs_json": json.dumps(legs),
            "notes": o.notes}


def fetch_mlb_schedule(cfg):
    if not cfg["mlbstats_enabled"]:
        return None
    from arb.venues.mlbstats import fetch_schedule
    today = datetime.now(NY).date()
    try:
        games = fetch_schedule(cfg, today.isoformat())
        return games
    except Exception as e:  # noqa: BLE001 — schedule is an upgrade, not a dependency
        log.warning("mlb schedule fetch failed: %s", e)
        return None


def persist_poll(cfg, store, poll_kind: str, all_quotes: list, venue_status: dict,
                 groups: list, opps: list, rejects: list,
                 started: datetime | None = None) -> int:
    """Shared persistence for any poll kind (watch and live use the same path)."""
    started = started or datetime.now(timezone.utc)
    poll_id = store.insert("polls", {
        "started_at": started.isoformat(), "kind": poll_kind,
        "n_quotes": len(all_quotes), "n_opps": len(opps)})
    store.insert_many("poll_venues", [
        {"poll_id": poll_id, "venue": v, "ok": st["ok"], "n_quotes": st["n"],
         "latency_ms": st["latency_ms"]} for v, st in venue_status.items()])
    store.insert_many("quotes", [_quote_row(q, poll_id) for q in all_quotes])
    store.insert_many("opportunities", [_opp_row(o, poll_id) for o in opps])
    store.insert_many("left_on_table", [
        {"poll_id": poll_id, "fingerprint": "", "kind": r["kind"],
         "event_key": r["event_key"], "venue_pair": r["venue_pair"],
         "legs_json": json.dumps(r["legs"]), "reason": r["reason"],
         "edge_points": r.get("edge_points")} for r in rejects])

    n_flag = sum(1 for o in opps if o.kind != "DIVERGENCE")
    console.print(f"[dim]poll {poll_id} ({poll_kind}): {len(all_quotes)} quotes, "
                  f"{len(groups)} events, {n_flag} locked/EV flags, "
                  f"{len(opps) - n_flag} divergences, {len(rejects)} rejects[/]")

    from arb.tracker import process_poll
    process_poll(cfg, store, poll_id, poll_kind, opps, venue_status)

    from arb.markout import persist_marks
    n_marks = persist_marks(cfg, store)
    if n_marks:
        log.info("persisted %d markout rows", n_marks)

    from arb.cli import head_sha
    from arb.heartbeat import beat
    beat(store, poll_kind, head_sha())
    return poll_id


def enrich_gate_ladders(cfg, opps, fetch=None) -> int:
    """Attach Kalshi ask-ladders to the covers that could actually pay.

    Kalshi has no batch book endpoint — a ladder costs one request PER MARKET —
    so fetching every market every poll would multiply Kalshi traffic ~100x and
    blow the poll cadence. Instead this runs after detect() and fetches only for
    opportunities that already cleared edge>0 and spread<=edge, which is the
    only population the depth gate ever scores (~5 in 9 days, measured).

    Why it exists: `depth_usd` is best-level size only, and until now every
    Kalshi quote stored ladder_json=NULL (0 of 26,990,797 rows), so
    capacity.py's `all(len(lad) >= 2)` could never pass on a Kalshi-legged cover
    and "died on depth" was never actually measured against a ladder.

    Mutates the leg Quotes in place, BEFORE persist_poll writes them, so nothing
    ever updates a stored row. Returns the number of ladders filled.
    """
    from arb import covers
    from arb.venues import kalshi
    if fetch is None:
        fetch = kalshi.fetch_orderbook
    filled = 0
    for o in opps:
        if o.edge is None or o.edge <= 0:
            continue
        sp = covers.half_spread_sum(
            [{"cost": q.cost, "bid": q.bid} for q in o.legs])
        if sp is None or sp > float(o.edge):
            continue                      # same gate covers.py scores on
        for q in o.legs:
            if q.venue != "kalshi" or q.ladder_json is not None:
                continue
            # A venue hiccup must not take the poll down: the ladder simply
            # stays absent, which the depth gate already reads as INSUFFICIENT.
            try:
                ob = fetch(cfg, q.venue_market_id)
            except Exception as e:  # noqa: BLE001
                log.warning("kalshi ladder fetch failed for %s: %s",
                            q.venue_market_id, e)
                continue
            lad = kalshi.ask_ladder(ob, q.venue_side or q.side)
            if lad:
                q.ladder_json = kalshi.ladder_json(lad)
                filled += 1
    return filled


def run_cycle(cfg, store, quotes_by_venue: dict, venue_status: dict,
              poll_kind: str = "watch") -> int:
    all_quotes = [q for qs in quotes_by_venue.values() for q in qs]
    groups = build_groups(all_quotes, cfg, load_aliases(),
                          mlb_schedule=fetch_mlb_schedule(cfg))
    opps, rejects = detect(cfg, groups)
    enrich_gate_ladders(cfg, opps)
    persist_poll(cfg, store, poll_kind, all_quotes, venue_status, groups, opps, rejects)
    return 0
