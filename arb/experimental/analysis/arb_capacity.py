"""Arb capacity report (offline, read-only).

Runs the microstructure ladder-walk (arb.capacity.max_economic_arb_size) over stored ARB
flags and answers the user's question directly: per opportunity, how many dollars you can
stake before the lock goes negative (break-even), the profit-max size, and whether EDGE or
DEPTH is the binding limit. Arbs logged before Wave 2 have no captured ladder and fall back to
a single top-of-book level (break-even ≈ top-of-book depth) — flagged as such.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from arb.capacity import max_economic_arb_size
from arb.store import Store


@dataclass(frozen=True)
class CapacityReport:
    n_arbs: int
    n_feasible: int
    n_with_ladder: int
    edge_limited: int
    depth_limited: int
    median_breakeven_usd: float | None
    median_profitmax_usd: float | None
    total_profitmax_profit_usd: float   # summed locked profit if each were sized at profit-max
    rows: list[dict[str, Any]]
    captured_fraction: float = 0.0      # n_with_ladder / n_feasible (B3/B4 real-depth coverage)
    insufficient_depth_data: bool = False  # captured_fraction < min_ladder_fraction -> UNKNOWN


def run_arb_capacity(store: Store, statuses: tuple[str, ...] = ("OPEN", "SETTLED"),
                     trusted_only: bool = True, min_ladder_fraction: float = 0.5,
                     fillable_fraction: Decimal = Decimal("1")) -> CapacityReport:
    placeholders = ",".join("?" for _ in statuses)
    # Audit B2 — only actionable arbs. trusted=1 drops the cross-venue REVIEW population
    # (hard rule #6) that otherwise makes ~99% of the summed-profit figure non-capturable.
    trust_clause = " AND trusted=1" if trusted_only else ""
    with store.connect() as con:
        bets = list(con.execute(
            f"SELECT id, fingerprint, legs_json, edge FROM flat_bets "
            f"WHERE kind='ARB' AND status IN ({placeholders}){trust_clause} ORDER BY id",
            statuses,
        ))
    # Audit B2 — dedup by fingerprint. The same arb re-observed across polls is one
    # opportunity, not N; summing over raw rows inflates capacity 4-5x. Keep the first
    # sighting of each fingerprint.
    seen: set[str] = set()
    deduped = []
    for bet in bets:
        fp = bet["fingerprint"]
        if fp in seen:
            continue
        seen.add(fp)
        deduped.append(bet)
    bets = deduped
    rows: list[dict[str, Any]] = []
    breakevens: list[float] = []
    profitmaxes: list[float] = []
    total_profit = 0.0
    edge_lim = depth_lim = with_ladder = feasible = 0
    for bet in bets:
        legs = json.loads(bet["legs_json"] or "[]")
        if len(legs) < 2:
            continue
        has_ladder = any(leg.get("ladder") for leg in legs)
        with_ladder += 1 if has_ladder else 0
        cap = max_economic_arb_size(legs, fillable_fraction)
        if not cap.feasible:
            continue
        feasible += 1
        edge_lim += 1 if cap.limited_by == "edge" else 0
        depth_lim += 1 if cap.limited_by == "depth" else 0
        breakevens.append(float(cap.breakeven_stake_usd))
        profitmaxes.append(float(cap.profitmax_stake_usd))
        total_profit += float(cap.max_profit_usd)
        rows.append({
            "id": int(bet["id"]),
            "edge": str(bet["edge"]),
            "breakeven_usd": float(cap.breakeven_stake_usd),
            "profitmax_usd": float(cap.profitmax_stake_usd),
            "max_profit_usd": float(cap.max_profit_usd),
            "limited_by": cap.limited_by,
            "has_ladder": has_ladder,
        })
    captured_fraction = (with_ladder / feasible) if feasible else 0.0
    # B3/B4: until enough feasible arbs carry a real multi-level ladder, every break-even/
    # profit number is top-of-book fiction (one price, infinitely refillable, edges that vanish
    # on re-quote). Mark the report UNKNOWN rather than emit a fantasy headline. G1: a feasible
    # population of 0 is its own explicit state — the sizing model has nothing to train on, so
    # flat $1 is the *correct* answer, surfaced rather than disguised as a real number.
    insufficient = feasible == 0 or captured_fraction < min_ladder_fraction
    return CapacityReport(
        n_arbs=len(bets), n_feasible=feasible, n_with_ladder=with_ladder,
        edge_limited=edge_lim, depth_limited=depth_lim,
        median_breakeven_usd=round(median(breakevens), 2) if breakevens else None,
        median_profitmax_usd=round(median(profitmaxes), 2) if profitmaxes else None,
        total_profitmax_profit_usd=round(total_profit, 2),
        rows=rows,
        captured_fraction=round(captured_fraction, 3),
        insufficient_depth_data=insufficient,
    )


def format_report(r: CapacityReport, top: int = 15) -> str:
    lines = [
        f"Arb capacity over {r.n_arbs} ARB flags ({r.n_feasible} feasible, {r.n_with_ladder} with a captured ladder):",
    ]
    # G1: no feasible trusted+settled arbs -> the sizing model has nothing to train on. Flat $1
    # is the correct output; say so plainly instead of returning a number that looks computed.
    if r.n_feasible == 0:
        lines.append(
            "  INSUFFICIENT_DEPTH_DATA: 0 feasible trusted arbs in this population — the depth/"
            "edge sizing model has no rows to fit, so the correct stake is flat $1 (not a "
            "computed break-even). This is the empty-population state, not a failure."
        )
        return "\n".join(lines)
    if r.insufficient_depth_data:
        lines += [
            f"  INSUFFICIENT_DEPTH_DATA: only {r.captured_fraction:.0%} of feasible arbs carry a "
            "real multi-level ladder (top-of-book only, NO slippage modeled).",
            "  Break-even / profit-max / summed profit = UNKNOWN until live captures accumulate "
            "(historical ladders can't be backfilled). Showing per-row depth coverage only:",
            f"  binding limit: {r.edge_limited} edge-limited, {r.depth_limited} depth-limited",
            f"  median break-even size : UNKNOWN",
            f"  median profit-max size : UNKNOWN",
            f"  summed locked profit if each sized at profit-max: UNKNOWN",
            "",
            f"  {'ID':>6} {'EDGE':>9} {'BREAK-EVEN$':>12} {'PROFITMAX$':>11} {'PROFIT$':>9}  LIMIT  LADDER",
        ]
        for row in sorted(r.rows, key=lambda x: x["max_profit_usd"], reverse=True)[:top]:
            be = f"{row['breakeven_usd']:>12.2f}" if row["has_ladder"] else f"{'UNKNOWN':>12}"
            pm = f"{row['profitmax_usd']:>11.2f}" if row["has_ladder"] else f"{'UNKNOWN':>11}"
            pf = f"{row['max_profit_usd']:>9.2f}" if row["has_ladder"] else f"{'UNKNOWN':>9}"
            lines.append(f"  {row['id']:>6} {row['edge']:>9} {be} {pm} {pf}  "
                         f"{row['limited_by']:>5}  {'yes' if row['has_ladder'] else 'top-only'}")
        return "\n".join(lines)
    lines += [
        f"  binding limit: {r.edge_limited} edge-limited, {r.depth_limited} depth-limited",
        f"  median break-even size : ${r.median_breakeven_usd}  (stake beyond this and the lock goes negative)",
        f"  median profit-max size : ${r.median_profitmax_usd}",
        f"  summed locked profit if each sized at profit-max: ${r.total_profitmax_profit_usd}",
        "",
        f"  {'ID':>6} {'EDGE':>9} {'BREAK-EVEN$':>12} {'PROFITMAX$':>11} {'PROFIT$':>9}  LIMIT  LADDER",
    ]
    for row in sorted(r.rows, key=lambda x: x["max_profit_usd"], reverse=True)[:top]:
        lines.append(f"  {row['id']:>6} {row['edge']:>9} {row['breakeven_usd']:>12.2f} "
                     f"{row['profitmax_usd']:>11.2f} {row['max_profit_usd']:>9.2f}  "
                     f"{row['limited_by']:>5}  {'yes' if row['has_ladder'] else 'top-only'}")
    if not r.rows:
        lines.append("  (no feasible arbs with usable depth yet)")
    return "\n".join(lines)
