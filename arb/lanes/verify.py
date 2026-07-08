"""Verify lane (SPEC §5 layout, addendum §4): flash re-quote of fresh locked
flags at 10s cadence. Measures arb lifetime/vanish rate directly AND doubles as
the paper leg-failure fill oracle: a re-quote that moved through the flagged
price counts as a simulated leg failure. SX legs are `unverifiable` (outcome
side is not re-derivable from stored rows) — marked, never silently skipped."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.logutil import get_logger

log = get_logger("arb.verify", "logs/scan.log")


def requote_leg(cfg, leg: dict) -> Decimal | None:
    """Current executable ask for one stored leg. None = unverifiable."""
    venue = leg.get("venue")
    vmid = leg.get("venue_market_id") or ""
    try:
        if venue == "kalshi":
            from arb.venues.kalshi import fetch_orderbook
            ob = fetch_orderbook(cfg, vmid)
            side = leg.get("venue_side", "YES")
            opp = "no_dollars" if side == "YES" else "yes_dollars"
            bids = ob.get(opp) or []
            if not bids:
                return None
            return 1 - max(Decimal(p) for p, _ in bids)
        if venue == "polymarket" and ":" in vmid:
            from arb.venues.polymarket import fetch_books
            token = vmid.split(":")[1]
            book = fetch_books(cfg, [token]).get(token)
            if not book or not book.get("asks"):
                return None
            return min(Decimal(a["price"]) for a in book["asks"])
    except Exception as e:  # noqa: BLE001
        log.warning("requote failed %s %s: %s", venue, vmid[:24], e)
    return None  # sx (and anything else): unverifiable per spec


def run_once(cfg, store, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    horizon = (now - timedelta(seconds=float(cfg["verify"]["max_bet_age_secs"])))
    bets = store.query(
        """SELECT fb.* FROM flat_bets fb
           WHERE fb.kind IN ('ARB','XSPREAD','POLITICAL','NEGRISK')
             AND fb.created_at > ?
             AND NOT EXISTS (SELECT 1 FROM annotations a WHERE a.target_kind='bet'
                             AND a.target_id = CAST(fb.id AS TEXT)
                             AND a.kind = 'verify')""", (horizon.isoformat(),))
    n = 0
    for bet in bets:
        legs = json.loads(bet["legs_json"])
        requotes = [requote_leg(cfg, leg) for leg in legs]
        age = (now - datetime.fromisoformat(bet["created_at"])).total_seconds()
        if any(r is None for r in requotes):
            verdict = {"verdict": "unverifiable",
                       "legs": [str(r) if r else None for r in requotes]}
        else:
            new_sum = sum(requotes)
            old_sum = sum(Decimal(l["cost"]) for l in legs)
            survives = new_sum < 1
            # paper fill oracle: a leg through its flagged price = simulated leg fail
            leg_fail = any(r > Decimal(l["cost"]) + cfg["execution"]["taker_price_buffer"]
                           for r, l in zip(requotes, legs))
            verdict = {"verdict": "survives" if survives else "vanished",
                       "age_secs": age, "requote_sum": str(new_sum),
                       "flagged_sum": str(old_sum), "simulated_leg_failure": leg_fail}
            if leg_fail:
                store.insert("bet_events", {
                    "bet_id": bet["id"], "created_at": now.isoformat(),
                    "kind": "leg_failure",
                    "payload_json": json.dumps({"source": "verify-requote",
                                                "requotes": [str(r) for r in requotes]})})
        store.insert("annotations", {
            "created_at": now.isoformat(), "target_kind": "bet",
            "target_id": str(bet["id"]), "kind": "verify",
            "detail": json.dumps(verdict)})
        n += 1
    return n
