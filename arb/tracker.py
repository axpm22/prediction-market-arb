"""The flat-stake tracker — THE deliverable (SPEC §10). flat_bets rows are
ENTRY-TIME FACTS ONLY and append-only (rule 9); everything later is a bet_events
row. Poll-kind ownership: only the poll kind that created a bet can close it
(bug I1). A venue outage carries lifetimes, never closes them (§10.2)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.eligibility import classify
from arb.logutil import get_logger
from arb.models import (LOCKED_KINDS, RESOLUTION_SETTLED_KINDS, Opportunity, q6,
                        position_key)

log = get_logger("arb.tracker", "logs/scan.log")


def candidate_from_opp(cfg, o: Opportunity) -> dict:
    venues = sorted({q.venue for q in o.legs})
    s = sum(q.effective_cost for q in o.legs)
    fetched = [q.fetched_at for q in o.legs]
    skew = (max(fetched) - min(fetched)).total_seconds() if fetched else 0.0
    depth_ok = all(q.depth_usd is not None and q.depth_usd >= cfg["min_executable_usd"]
                   for q in o.legs)
    payout = Decimal(1)
    legs = [{"venue": q.venue, "book": q.book, "market_type": q.market_type,
             "line": q.line, "outcome": q.outcome_label, "side": q.side,
             "venue_side": q.venue_side or q.side,
             "cost": str(q.cost), "fee_cost": str(q.fee_cost),
             "mid0": str(q6((q.cost + q.bid) / 2)) if q.bid is not None else None,
             "venue_market_id": q.venue_market_id,
             "fetched_at": q.fetched_at.isoformat()} for q in o.legs]
    first = o.legs[0]
    return {
        "kind": o.kind, "category": o.category, "sport": o.event.sport,
        "event_key": o.event.event_key, "event_start_utc": o.event.start_utc,
        "start_grade": o.event.start_grade,
        "venue_pair": ":".join(venues) if len(venues) > 1 else f"{venues[0]}:self",
        "fingerprint": o.fingerprint,
        "position_key": position_key(o.kind, o.event.sport, o.event.event_key,
                                     first.market_type, first.line,
                                     outcome=first.outcome_label),
        "legs": legs, "edge_points": o.edge, "cost_sum": q6(s), "payout": payout,
        "roi": q6(payout / s - 1) if s else None, "status": o.status, "notes": o.notes,
        "quality_flag": "none", "leg_skew_secs": skew, "depth_ok": depth_ok,
        "proof_status": None,
    }


def candidate_from_basket(b: dict, endale=None) -> dict:
    return {
        "kind": b["kind"], "category": "nonsports", "sport": "",
        "event_key": b["event_key"], "event_start_utc": None, "start_grade": "absent",
        "venue_pair": b["venue_pair"], "fingerprint": b["fingerprint"],
        "position_key": b["position_key"], "legs": json.loads(b["legs_json"]),
        "edge_points": b["edge_points"], "cost_sum": b["cost_sum"],
        "payout": b["payout"], "roi": b["roi"], "status": b["status"],
        "notes": b["notes"], "quality_flag": "none", "leg_skew_secs": 0.0,
        "depth_ok": True, "proof_status": b["proof_status"],
    }


def _active_bets(store, poll_kind: str):
    return store.query(
        """SELECT fb.* FROM flat_bets fb
           WHERE fb.poll_kind = ?
             AND NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id = fb.id
                             AND be.kind IN ('lifetime_closed', 'void'))
             AND NOT (fb.kind IN ('EV', 'NEGRISK', 'BTC')
                      AND EXISTS (SELECT 1 FROM bet_events be2
                                  WHERE be2.bet_id = fb.id AND be2.kind = 'settled'))""",
        (poll_kind,))


def _lot(store, poll_id, cand, reason):
    store.insert("left_on_table", {
        "poll_id": poll_id, "fingerprint": cand["fingerprint"], "kind": cand["kind"],
        "event_key": cand["event_key"], "venue_pair": cand["venue_pair"],
        "legs_json": json.dumps(cand["legs"]), "reason": reason,
        "edge_points": cand["edge_points"]})


def _book(cfg, store, poll_id, poll_kind, cand, lane, reason, now,
          reappearance_of=None):
    pnl_optimistic = None
    if cand["kind"] in LOCKED_KINDS:  # locked P&L IS pnl_optimistic, known at entry
        pnl_optimistic = q6(cand["payout"] / Decimal(cand["cost_sum"]) - 1)
    trusted = cand["venue_pair"].endswith(":self") or \
        cand["venue_pair"] in [str(p) for p in cfg["trusted_pairs"]]
    bet_id = store.insert("flat_bets", {
        "created_at": now.isoformat(), "poll_id": poll_id, "poll_kind": poll_kind,
        "fingerprint": cand["fingerprint"], "position_key": cand["position_key"],
        "kind": cand["kind"], "category": cand["category"], "sport": cand["sport"],
        "venue_pair": cand["venue_pair"], "event_key": cand["event_key"],
        "event_start_utc": cand["event_start_utc"].isoformat()
            if cand["event_start_utc"] else None,
        "start_grade": cand["start_grade"], "legs_json": json.dumps(cand["legs"]),
        "stake_usd": Decimal(1), "edge_points": cand["edge_points"],
        "trusted": trusted, "lane": lane, "quality_flag": cand["quality_flag"],
        "consensus_age_secs": cand.get("consensus_age_secs"),
        "leg_skew_secs": cand["leg_skew_secs"], "status": cand["status"],
        "pnl_optimistic": pnl_optimistic, "reappearance_of": reappearance_of,
        "notes": f"{reason}; {cand['notes']}".strip("; ")})
    if cand["kind"] in LOCKED_KINDS and cand["kind"] not in RESOLUTION_SETTLED_KINDS:
        # ARB/XSPREAD/POLITICAL settle at entry by design (§11.1, L1) — exempt
        # from the instant-settle tripwire; NEGRISK/BTC wait for resolution
        store.insert("bet_events", {
            "bet_id": bet_id, "created_at": now.isoformat(), "kind": "settled",
            "payload_json": json.dumps({"result": "LOCKED_AT_ENTRY",
                                        "source": "entry",
                                        "pnl": str(pnl_optimistic)})})
    return bet_id


def process_poll(cfg, store, poll_id: int, poll_kind: str, opps: list,
                 venue_status: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    cands = []
    for o in opps:
        c = candidate_from_opp(cfg, o) if isinstance(o, Opportunity) else o
        if c["kind"] != "DIVERGENCE":  # divergences NEVER create ledger rows (§10.1)
            cands.append(c)
    sighted = {c["fingerprint"]: c for c in cands}
    active = _active_bets(store, poll_kind)
    active_fps = {b["fingerprint"] for b in active}
    active_positions = {b["position_key"]: b for b in active}

    # 1) lifetime + confirmation for existing bets
    for bet in active:
        if bet["fingerprint"] in sighted:
            cand = sighted[bet["fingerprint"]]
            has_confirm = store.query(
                "SELECT 1 FROM bet_events WHERE bet_id=? AND kind='confirmed'",
                (bet["id"],))
            if not has_confirm:
                start = bet["event_start_utc"]
                pregame = start is None or datetime.fromisoformat(start) > now
                if pregame:
                    store.insert("bet_events", {
                        "bet_id": bet["id"], "created_at": now.isoformat(),
                        "kind": "confirmed",
                        "payload_json": json.dumps({
                            "poll_id": poll_id, "cost_sum": str(cand["cost_sum"]),
                            "legs": cand["legs"]})})
                else:
                    # an in-play second sighting never confirms (v3.4) — the bet
                    # counts missed; annotation records why at read time
                    store.insert("annotations", {
                        "created_at": now.isoformat(), "target_kind": "bet",
                        "target_id": str(bet["id"]), "kind": "in-play-at-confirm",
                        "detail": f"poll {poll_id}"})
        else:
            leg_venues = {leg["venue"] for leg in json.loads(bet["legs_json"])}
            gap = [v for v in leg_venues if not venue_status.get(v, {}).get("ok")]
            if gap:
                store.insert("annotations", {
                    "created_at": now.isoformat(), "target_kind": "poll",
                    "target_id": str(poll_id), "kind": "venue-fetch-gap",
                    "detail": f"bet {bet['id']} carried; venues down: {gap}"})
            else:
                store.insert("bet_events", {
                    "bet_id": bet["id"], "created_at": now.isoformat(),
                    "kind": "lifetime_closed",
                    "payload_json": json.dumps({"reason": "absent-one-poll",
                                                "poll_id": poll_id})})

    # 2) bet creation for new sightings
    for cand in cands:
        if cand["fingerprint"] in active_fps:
            continue  # sighting handled above
        if cand["kind"] == "EV" and not cfg["ev_enabled"]:
            _lot(store, poll_id, cand, "ev-frozen")  # frozen: record, never book
            continue
        if cand["kind"] == "EV":
            age = cand.get("consensus_age_secs")
            bound = float(cfg["consensus_pregame_max_age_hours"]) * 3600
            if age is not None and age > bound:
                _lot(store, poll_id, cand, "stale-consensus")  # booking rule v3.4
                continue
            edge = Decimal(str(cand["edge_points"]))
            if edge > cfg["ev"]["max_edge"]:
                _lot(store, poll_id, cand, "edge-implausible")
                continue
        if store.query("SELECT 1 FROM blacklist WHERE fingerprint=?",
                       (cand["fingerprint"],)):
            _lot(store, poll_id, cand, "blacklisted")
            continue
        if cand["kind"] in RESOLUTION_SETTLED_KINDS and cand["event_start_utc"]:
            horizon = int(cfg["nonsports_max_days"] if cand["category"] != "sports"
                          else cfg["max_days_to_settlement"])
            if cand["event_start_utc"] > now + timedelta(days=horizon):
                _lot(store, poll_id, cand, "too-far-to-settlement")
                continue
        pos = active_positions.get(cand["position_key"])
        if pos:
            # same economic position: absorb as improved quote, never a new bet (§10.1)
            store.insert("annotations", {
                "created_at": now.isoformat(), "target_kind": "bet",
                "target_id": str(pos["id"]), "kind": "position-absorb",
                "detail": json.dumps({"poll_id": poll_id,
                                      "cost_sum": str(cand["cost_sum"]),
                                      "venue_pair": cand["venue_pair"]})})
            if cand["venue_pair"] != pos["venue_pair"]:
                _lot(store, poll_id, cand, "position-dedup")  # flag count stays honest
            continue

        pregame = cand["event_start_utc"] is None or cand["event_start_utc"] > now
        eligible, reason, lane = classify({**cand, "pregame": pregame,
                                           "micro": False, "ai_matched": False,
                                           "live": False}, cfg)
        prior = store.query(
            "SELECT id FROM flat_bets WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
            (cand["fingerprint"],))
        reappearance_of = prior[0]["id"] if prior else None
        n24 = store.query(
            "SELECT COUNT(*) c FROM flat_bets WHERE fingerprint=? AND created_at > ?",
            (cand["fingerprint"], (now - timedelta(hours=24)).isoformat()))[0]["c"]
        if n24 >= int(cfg["tracker"]["max_reappearances"]):
            log.warning("fingerprint %s exceeded max_reappearances (%d rows/24h)",
                        cand["fingerprint"], n24)
        _book(cfg, store, poll_id, poll_kind, cand, lane, reason, now,
              reappearance_of=reappearance_of)
