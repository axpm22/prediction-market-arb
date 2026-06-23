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


def run_arb_capacity(store: Store, statuses: tuple[str, ...] = ("OPEN", "SETTLED")) -> CapacityReport:
    placeholders = ",".join("?" for _ in statuses)
    with store.connect() as con:
        bets = list(con.execute(
            f"SELECT id, legs_json, edge FROM flat_bets WHERE kind='ARB' AND status IN ({placeholders}) ORDER BY id",
            statuses,
        ))
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
        cap = max_economic_arb_size(legs)
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
    return CapacityReport(
        n_arbs=len(bets), n_feasible=feasible, n_with_ladder=with_ladder,
        edge_limited=edge_lim, depth_limited=depth_lim,
        median_breakeven_usd=round(median(breakevens), 2) if breakevens else None,
        median_profitmax_usd=round(median(profitmaxes), 2) if profitmaxes else None,
        total_profitmax_profit_usd=round(total_profit, 2),
        rows=rows,
    )


def format_report(r: CapacityReport, top: int = 15) -> str:
    lines = [
        f"Arb capacity over {r.n_arbs} ARB flags ({r.n_feasible} feasible, {r.n_with_ladder} with a captured ladder):",
    ]
    if r.n_feasible and r.n_with_ladder / r.n_feasible < 0.5:
        lines.append(
            "  WARNING: most arbs have NO captured ladder (logged before Wave 2) -> single "
            "top-of-book level, NO slippage modeled. These break-evens/profits OVERSTATE real "
            "capacity (one price, infinitely refillable) and ignore that edges vanish on re-quote. "
            "Real numbers accrue once ladder-captured arbs are logged."
        )
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
