"""Capacity (SPEC §13.4, §18.4) — projections only, NEVER headline.

Reports INSUFFICIENT_DEPTH_DATA until >= execution.min_ladder_fraction of the
day's feasible locked flags (deduped by position_key — V1 bug B2's 4.5x
duplication) carry real multi-level ladders on every leg. Above the gate,
projections cover trusted+clean rows only (99% REVIEW rows were the other
half of the "$72,209" fantasy), priced at ladder VWAP via arb/sizing.py.
"""
from __future__ import annotations

import json
from decimal import Decimal

from arb import sizing
from arb.models import LOCKED_KINDS, q6

INSUFFICIENT = "INSUFFICIENT_DEPTH_DATA"


def _leg_quote(store, poll_id, event_key, leg) -> dict | None:
    rows = store.query(
        """SELECT ladder_json, depth_usd FROM quotes
           WHERE poll_id=? AND venue=? AND event_key=? AND market_type=?
             AND outcome=? AND side=? AND (line IS ? OR line=?) LIMIT 1""",
        (poll_id, leg["venue"], event_key, leg["market_type"],
         leg["outcome"], leg["side"], leg["line"], leg["line"]))
    return dict(rows[0]) if rows else None


def capacity_frame(cfg, store, day: str) -> dict:
    from arb.metrics import day_bounds
    start, end = day_bounds(cfg, day)
    ks = ",".join("?" * len(LOCKED_KINDS))
    bets = [dict(r) for r in store.query(
        f"SELECT * FROM flat_bets WHERE created_at >= ? AND created_at < ? "
        f"AND kind IN ({ks}) ORDER BY id", (start, end, *LOCKED_KINDS))]
    seen: set[str] = set()
    feasible = []
    for b in bets:
        if b["position_key"] in seen:
            continue
        seen.add(b["position_key"])
        legs = json.loads(b["legs_json"])
        quotes = [_leg_quote(store, b["poll_id"], b["event_key"], lg) for lg in legs]
        ladders = [sizing.parse_ladder(q["ladder_json"]) if q else [] for q in quotes]
        has_ladders = (bool(ladders) and all(q is not None for q in quotes)
                       and all(len(lad) >= 2 for lad in ladders))
        feasible.append((b, legs, quotes, ladders, has_ladders))
    n = len(feasible)
    n_ladders = sum(1 for f in feasible if f[4])
    fraction = n_ladders / n if n else 0.0
    frame = {"status": INSUFFICIENT, "ladder_fraction": round(fraction, 4),
             "n_feasible": n, "n_with_ladders": n_ladders,
             "positions": [], "projected_pnl": Decimal(0)}
    if n == 0 or fraction < float(cfg["execution"]["min_ladder_fraction"]):
        return frame  # capacity says UNKNOWN below the gate (§18.4)
    frame["status"] = "OK"
    for b, legs, quotes, ladders, has_ladders in feasible:
        if not has_ladders or not b["trusted"] or b["lane"] != "clean":
            continue
        proj = sizing.arb_projection(cfg, legs, quotes, ladders)
        if proj:
            proj["position_key"] = b["position_key"]
            proj["kind"] = b["kind"]
            frame["positions"].append(proj)
            frame["projected_pnl"] += proj["pnl_usd"]
    frame["projected_pnl"] = q6(frame["projected_pnl"])
    return frame
