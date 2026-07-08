"""Markouts (SPEC §10.5): persist the BOOK MID at +30s/+2m/+10m/+30m per booked
leg, from quotes the loops already sample. Mid, not ask (ask double-counts
half-spread). A mark uses the nearest sample within ±50% of its horizon and
records the actual offset; no sample in tolerance -> stays absent (NULL row is
never written — coverage is reported instead). Post-start marks are stored but
tagged in_play and excluded from pnl_markout (§10.5)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from arb.models import q6

HORIZONS = (30, 120, 600, 1800)


def persist_marks(cfg, store, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    window_start = (now - timedelta(seconds=HORIZONS[-1] * 2)).isoformat()
    bets = store.query("SELECT * FROM flat_bets WHERE created_at > ?", (window_start,))
    n = 0
    for bet in bets:
        placed = datetime.fromisoformat(bet["created_at"])
        start = datetime.fromisoformat(bet["event_start_utc"]) \
            if bet["event_start_utc"] else None
        legs = json.loads(bet["legs_json"])
        have = {(r["leg_index"], r["horizon_secs"]) for r in store.query(
            "SELECT leg_index, horizon_secs FROM markouts WHERE bet_id=?", (bet["id"],))}
        for i, leg in enumerate(legs):
            for h in HORIZONS:
                if (i, h) in have:
                    continue
                target = placed + timedelta(seconds=h)
                if now < target - timedelta(seconds=h * 0.5):
                    continue  # horizon not reachable yet
                lo = (target - timedelta(seconds=h * 0.5)).isoformat()
                hi = (target + timedelta(seconds=h * 0.5)).isoformat()
                rows = store.query(
                    """SELECT fetched_at, cost, bid FROM quotes
                       WHERE event_key=? AND venue=? AND market_type=?
                         AND COALESCE(line,'')=? AND outcome=? AND side=?
                         AND bid IS NOT NULL AND fetched_at BETWEEN ? AND ?""",
                    (bet["event_key"], leg["venue"], leg["market_type"],
                     leg.get("line") or "", leg["outcome"], leg["side"], lo, hi))
                if not rows:
                    continue
                best = min(rows, key=lambda r: abs(
                    (datetime.fromisoformat(r["fetched_at"]) - target).total_seconds()))
                at = datetime.fromisoformat(best["fetched_at"])
                from decimal import Decimal
                mid = (Decimal(best["cost"]) + Decimal(best["bid"])) / 2
                store.insert("markouts", {
                    "bet_id": bet["id"], "leg_index": i, "horizon_secs": h,
                    "actual_offset_secs": (at - placed).total_seconds(),
                    "mark_cost": q6(mid), "marked_at": best["fetched_at"],
                    "in_play": bool(start and at >= start)})
                n += 1
    return n
