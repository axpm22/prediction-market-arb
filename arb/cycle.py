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
    }


def _opp_row(o: Opportunity, poll_id):
    legs = [{"venue": q.venue, "book": q.book, "market_type": q.market_type,
             "line": q.line, "outcome": q.outcome_label, "side": q.side,
             "cost": str(q.cost), "fee_cost": str(q.fee_cost),
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


def run_cycle(cfg, store, quotes_by_venue: dict, venue_status: dict,
              poll_kind: str = "watch") -> int:
    started = datetime.now(timezone.utc)
    all_quotes = [q for qs in quotes_by_venue.values() for q in qs]
    groups = build_groups(all_quotes, cfg, load_aliases(),
                          mlb_schedule=fetch_mlb_schedule(cfg))
    opps, rejects = detect(cfg, groups)

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
    return 0
