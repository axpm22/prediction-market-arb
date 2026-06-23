"""Depth-aware ARB sizing projection.

The training population is intentionally narrow: only ARB rows that were actually
paper-bet and later settled. Verification/lifetime/vanished annotations are useful
for safety gates, but they are not realized bets and must not train the sizing model.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .pricing import money


DEPTH_BUCKETS = (
    (Decimal("0"), Decimal("100"), "<$100"),
    (Decimal("100"), Decimal("250"), "$100-250"),
    (Decimal("250"), Decimal("500"), "$250-500"),
    (Decimal("500"), Decimal("1000"), "$500-1k"),
    (Decimal("1000"), Decimal("5000"), "$1k-5k"),
    (Decimal("5000"), None, "$5k+"),
)

EDGE_BUCKETS = (
    (Decimal("0"), Decimal("0.0025"), "<0.25%"),
    (Decimal("0.0025"), Decimal("0.005"), "0.25-0.5%"),
    (Decimal("0.005"), Decimal("0.01"), "0.5-1%"),
    (Decimal("0.01"), Decimal("0.02"), "1-2%"),
    (Decimal("0.02"), Decimal("0.05"), "2-5%"),
    (Decimal("0.05"), None, "5%+"),
)

EDGE_CAPS = {
    "0.5-1%": Decimal("5"),
    "1-2%": Decimal("15"),
    "2-5%": Decimal("50"),
    "5%+": Decimal("100"),
}


@dataclass
class _Stats:
    n: int = 0
    stake: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")

    def add(self, stake: Decimal, pnl: Decimal) -> None:
        self.n += 1
        self.stake += stake
        self.pnl += pnl

    def as_json(self) -> dict[str, Any]:
        roi = self.pnl / self.stake if self.stake else Decimal("0")
        return {
            "n": self.n,
            "stake": float(self.stake),
            "pnl": float(self.pnl),
            "roi": float(roi),
        }


def build_arb_sizing_model(con, bankroll: Decimal, max_stake_pct: Decimal,
                           min_sample: int = 20) -> dict[str, Any]:
    """Train from settled, staked ARB rows only."""
    rows = con.execute(
        """
        SELECT *
          FROM flat_bets
         WHERE kind='ARB'
           AND status='SETTLED'
           AND CAST(stake_usd AS REAL) > 0
           AND pnl_optimistic IS NOT NULL
        """
    ).fetchall()
    total = _Stats()
    by_depth: dict[str, _Stats] = defaultdict(_Stats)
    by_edge: dict[str, _Stats] = defaultdict(_Stats)
    by_pair: dict[str, _Stats] = defaultdict(_Stats)
    for row in rows:
        stake = _dec(row["stake_usd"])
        pnl = _dec(row["pnl_optimistic"])
        legs = _legs(row["legs_json"])
        binding_depth = binding_depth_usd(legs)
        edge = edge_points(row, legs)
        pair = venue_pair(legs)
        total.add(stake, pnl)
        by_depth[_bucket(binding_depth, DEPTH_BUCKETS)].add(stake, pnl)
        by_edge[_bucket(edge, EDGE_BUCKETS)].add(stake, pnl)
        by_pair[pair].add(stake, pnl)
    return {
        "rule": "settled staked ARBs only; stake scales with binding top-of-book depth after empirical gates",
        "training": total.as_json(),
        "min_sample": min_sample,
        "market_depth_fraction": 0.01,
        "max_stake": float(money(bankroll * max_stake_pct)),
        "depth": _stats_map(by_depth),
        "edge": _stats_map(by_edge),
        "venue_pair": _stats_map(by_pair),
    }


def recommend_arb_stake(legs: list[dict[str, Any]], row_edge_points: Any,
                        model: dict[str, Any]) -> dict[str, Any]:
    binding_depth = binding_depth_usd(legs)
    edge = _maybe_dec(row_edge_points)
    if edge is None:
        edge = edge_points({}, legs)
    pair = venue_pair(legs)
    depth_bucket = _bucket(binding_depth, DEPTH_BUCKETS)
    edge_bucket = _bucket(edge, EDGE_BUCKETS)
    max_stake = _dec(model.get("max_stake", "0"))
    reasons: list[str] = []
    stake = Decimal("1.00")

    if binding_depth is None:
        reasons.append("no depth on one or more legs")
        return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=False)
    if edge is None or edge < Decimal("0.005"):
        reasons.append("past bet-on ARBs below 0.5% edge did not justify scaling")
        return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=False)

    min_sample = int(model.get("min_sample", 20))
    pair_stats = _lookup(model, "venue_pair", pair)
    depth_stats = _lookup(model, "depth", depth_bucket)
    edge_stats = _lookup(model, "edge", edge_bucket)
    for label, stats in (("venue pair", pair_stats), ("depth bucket", depth_stats), ("edge bucket", edge_stats)):
        if stats["n"] < min_sample:
            reasons.append(f"{label} has too few settled staked ARBs")
        elif Decimal(str(stats["roi"])) < Decimal("0.01"):
            reasons.append(f"{label} realized ROI below 1%")
    if reasons:
        return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=False)

    edge_cap = EDGE_CAPS.get(edge_bucket)
    if edge_cap is None:
        reasons.append("edge bucket has no scale cap")
        return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=False)

    depth_stake = binding_depth * Decimal(str(model.get("market_depth_fraction", 0.01)))
    stake = money(min(max_stake, edge_cap, depth_stake))
    if stake < Decimal("1.00"):
        stake = Decimal("1.00")
    reasons.append("scaled from settled staked ARB depth/edge/pair buckets")
    return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=True)


def binding_depth_usd(legs: list[dict[str, Any]]) -> Decimal | None:
    depths = [_maybe_dec(leg.get("depth_usd")) for leg in legs]
    depths = [depth for depth in depths if depth is not None]
    return min(depths) if depths and len(depths) == len(legs) else None


def venue_pair(legs: list[dict[str, Any]]) -> str:
    venues = sorted({str(leg.get("venue") or "unknown") for leg in legs})
    return "+".join(venues) if venues else "unknown"


def edge_points(row: Any, legs: list[dict[str, Any]]) -> Decimal | None:
    raw = row.get("edge_points") if hasattr(row, "get") else None
    edge = _maybe_dec(raw)
    if edge is not None:
        return edge
    basket = _maybe_dec(row.get("optimistic_cost")) if hasattr(row, "get") else None
    if basket is None:
        costs = [_maybe_dec(leg.get("cost")) or Decimal("0") for leg in legs]
        fees = [_maybe_dec(leg.get("fee_cost")) or Decimal("0") for leg in legs]
        basket = sum(costs, Decimal("0")) + sum(fees, Decimal("0"))
    return Decimal("1") - basket if basket else None


def _recommendation(stake: Decimal, binding_depth: Decimal | None, edge: Decimal | None,
                    pair: str, depth_bucket: str, edge_bucket: str, reasons: list[str],
                    scalable: bool) -> dict[str, Any]:
    return {
        "stake": float(money(stake)),
        "scalable": scalable,
        "binding_depth": None if binding_depth is None else float(binding_depth),
        "edge_points": None if edge is None else float(edge),
        "venue_pair": pair,
        "depth_bucket": depth_bucket,
        "edge_bucket": edge_bucket,
        "reason": "; ".join(reasons),
    }


def _lookup(model: dict[str, Any], group: str, key: str) -> dict[str, Any]:
    return dict(model.get(group, {}).get(key) or {"n": 0, "stake": 0, "pnl": 0, "roi": 0})


def _stats_map(stats: dict[str, _Stats]) -> dict[str, dict[str, Any]]:
    return {key: value.as_json() for key, value in sorted(stats.items())}


def _bucket(value: Decimal | None, buckets: tuple[tuple[Decimal, Decimal | None, str], ...]) -> str:
    if value is None:
        return "unknown"
    for lower, upper, label in buckets:
        if value >= lower and (upper is None or value < upper):
            return label
    return "unknown"


def _legs(raw: str | None) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _dec(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _maybe_dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - defensive parse for external venue payloads.
        return None
