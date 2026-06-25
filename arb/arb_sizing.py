"""Depth-aware ARB sizing projection.

The training population is intentionally narrow: only ARB rows that were actually
paper-bet, later settled, AND trusted=1 (a whitelisted venue-pair per
config.trusted_pairs). Un-whitelisted cross-venue arbs are REVIEW-only (hard rule #6):
their "locks" are unverified across rulebooks, so they must never train sizing.
Verification/lifetime/vanished annotations are useful for safety gates, but they are
not realized bets and must not train the sizing model either.
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

# G2: the constants above are the DEFAULTS; config.arb_sizing overrides them. _resolve_sizing_cfg
# turns the raw config block into effective values, and the resolved buckets/caps/thresholds are
# stored into the model dict so recommend_arb_stake stays consistent with how the model was built.
_DEFAULT_MIN_SAMPLE = 20
_DEFAULT_TARGET_STAKE = Decimal("10")
_DEFAULT_MARKET_DEPTH_FRACTION = Decimal("0.01")
_DEFAULT_MIN_EDGE_TO_SCALE = Decimal("0.005")
_DEFAULT_MIN_BUCKET_ROI = Decimal("0.01")


def _parse_buckets(raw, default):
    if not raw:
        return default
    out = []
    for lower, upper, label in raw:
        hi = None if upper in (None, "", "null") else Decimal(str(upper))
        out.append((Decimal(str(lower)), hi, str(label)))
    return tuple(out)


def _serialize_buckets(buckets) -> list:
    return [[str(lo), None if hi is None else str(hi), label] for lo, hi, label in buckets]


def _resolve_sizing_cfg(cfg: dict | None) -> dict:
    cfg = cfg or {}
    edge_caps = {str(k): Decimal(str(v)) for k, v in (cfg.get("edge_caps") or {}).items()} or dict(EDGE_CAPS)
    return {
        "depth_buckets": _parse_buckets(cfg.get("depth_buckets"), DEPTH_BUCKETS),
        "edge_buckets": _parse_buckets(cfg.get("edge_buckets"), EDGE_BUCKETS),
        "edge_caps": edge_caps,
        "market_depth_fraction": Decimal(str(cfg.get("market_depth_fraction", _DEFAULT_MARKET_DEPTH_FRACTION))),
        "min_sample": int(cfg.get("min_sample", _DEFAULT_MIN_SAMPLE)),
        "target_stake": Decimal(str(cfg.get("target_stake", _DEFAULT_TARGET_STAKE))),
        "min_edge_to_scale": Decimal(str(cfg.get("min_edge_to_scale", _DEFAULT_MIN_EDGE_TO_SCALE))),
        "min_bucket_roi": Decimal(str(cfg.get("min_bucket_roi", _DEFAULT_MIN_BUCKET_ROI))),
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


@dataclass
class _ScaleStats:
    n: int = 0
    scaled_n: int = 0
    actual_stake: Decimal = Decimal("0")
    actual_pnl: Decimal = Decimal("0")
    flat10_stake: Decimal = Decimal("0")
    flat10_pnl: Decimal = Decimal("0")
    depth_stake: Decimal = Decimal("0")
    depth_pnl: Decimal = Decimal("0")
    max_flat10_stake: Decimal = Decimal("0")
    max_depth_stake: Decimal = Decimal("0")

    def add(self, actual_stake: Decimal, actual_pnl: Decimal,
            flat10_stake: Decimal, flat10_pnl: Decimal,
            depth_stake: Decimal, depth_pnl: Decimal) -> None:
        self.n += 1
        if flat10_stake > actual_stake or depth_stake > actual_stake:
            self.scaled_n += 1
        self.actual_stake += actual_stake
        self.actual_pnl += actual_pnl
        self.flat10_stake += flat10_stake
        self.flat10_pnl += flat10_pnl
        self.depth_stake += depth_stake
        self.depth_pnl += depth_pnl
        self.max_flat10_stake = max(self.max_flat10_stake, flat10_stake)
        self.max_depth_stake = max(self.max_depth_stake, depth_stake)

    def as_json(self) -> dict[str, Any]:
        actual_roi = self.actual_pnl / self.actual_stake if self.actual_stake else Decimal("0")
        flat10_roi = self.flat10_pnl / self.flat10_stake if self.flat10_stake else Decimal("0")
        depth_roi = self.depth_pnl / self.depth_stake if self.depth_stake else Decimal("0")
        return {
            "n": self.n,
            "scaled_n": self.scaled_n,
            "actual_stake": float(money(self.actual_stake)),
            "actual_pnl": float(money(self.actual_pnl)),
            "actual_roi": float(actual_roi),
            "flat10_stake": float(money(self.flat10_stake)),
            "flat10_pnl": float(money(self.flat10_pnl)),
            "flat10_roi": float(flat10_roi),
            "flat10_lift": float(money(self.flat10_pnl - self.actual_pnl)),
            "depth_stake": float(money(self.depth_stake)),
            "depth_pnl": float(money(self.depth_pnl)),
            "depth_roi": float(depth_roi),
            "depth_lift": float(money(self.depth_pnl - self.actual_pnl)),
            "max_flat10_stake": float(money(self.max_flat10_stake)),
            "max_depth_stake": float(money(self.max_depth_stake)),
        }


def build_arb_sizing_model(con, bankroll: Decimal, max_stake_pct: Decimal,
                           min_sample: int | None = None, cfg: dict | None = None) -> dict[str, Any]:
    """Train from settled, staked ARB rows only."""
    resolved = _resolve_sizing_cfg(cfg)
    depth_buckets = resolved["depth_buckets"]
    edge_buckets = resolved["edge_buckets"]
    effective_min_sample = min_sample if min_sample is not None else resolved["min_sample"]
    rows = con.execute(
        """
        SELECT *
          FROM flat_bets
         WHERE kind='ARB'
           AND status='SETTLED'
           AND trusted=1
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
        by_depth[_bucket(binding_depth, depth_buckets)].add(stake, pnl)
        by_edge[_bucket(edge, edge_buckets)].add(stake, pnl)
        by_pair[pair].add(stake, pnl)
    return {
        "rule": "trusted (whitelisted) settled staked ARBs only; stake scales with binding top-of-book depth after empirical gates",
        "training": total.as_json(),
        "min_sample": effective_min_sample,
        "market_depth_fraction": float(resolved["market_depth_fraction"]),
        # G2: carry the resolved knobs so recommend_arb_stake bins/caps a live arb exactly as
        # this model was trained, even if config changes between build and recommend.
        "min_edge_to_scale": float(resolved["min_edge_to_scale"]),
        "min_bucket_roi": float(resolved["min_bucket_roi"]),
        "edge_caps": {k: float(v) for k, v in resolved["edge_caps"].items()},
        "depth_buckets": _serialize_buckets(depth_buckets),
        "edge_buckets": _serialize_buckets(edge_buckets),
        "max_stake": float(money(bankroll * max_stake_pct)),
        "depth": _stats_map(by_depth),
        "edge": _stats_map(by_edge),
        "venue_pair": _stats_map(by_pair),
    }


def build_arb_scale_projection(con, bankroll: Decimal, max_stake_pct: Decimal,
                               target_stake: Decimal | None = None,
                               market_depth_fraction: Decimal | None = None,
                               cfg: dict | None = None) -> dict[str, Any]:
    """Counterfactual larger-stake P&L for settled ARBs that were actually bet.

    This is not a fill simulator. It keeps each settled ARB's observed per-dollar
    result and only changes stake when recorded top-of-book depth could plausibly
    absorb the larger bet.
    """
    resolved = _resolve_sizing_cfg(cfg)
    depth_buckets = resolved["depth_buckets"]
    edge_buckets = resolved["edge_buckets"]
    edge_caps = resolved["edge_caps"]
    if target_stake is None:
        target_stake = resolved["target_stake"]
    if market_depth_fraction is None:
        market_depth_fraction = resolved["market_depth_fraction"]
    rows = con.execute(
        """
        SELECT *
          FROM flat_bets
         WHERE kind='ARB'
           AND status='SETTLED'
           AND trusted=1
           AND CAST(stake_usd AS REAL) > 0
           AND pnl_optimistic IS NOT NULL
        """
    ).fetchall()
    max_stake = money(bankroll * max_stake_pct)
    total = _ScaleStats()
    by_depth: dict[str, _ScaleStats] = defaultdict(_ScaleStats)
    by_edge: dict[str, _ScaleStats] = defaultdict(_ScaleStats)
    by_pair: dict[str, _ScaleStats] = defaultdict(_ScaleStats)
    examples: list[dict[str, Any]] = []
    no_depth = 0
    for row in rows:
        actual_stake = _dec(row["stake_usd"])
        if actual_stake <= 0:
            continue
        actual_pnl = _dec(row["pnl_optimistic"])
        roi_per_dollar = actual_pnl / actual_stake
        legs = _legs(row["legs_json"])
        binding_depth = binding_depth_usd(legs)
        edge = edge_points(row, legs)
        pair = venue_pair(legs)
        depth_bucket = _bucket(binding_depth, depth_buckets)
        edge_bucket = _bucket(edge, edge_buckets)
        if binding_depth is None:
            no_depth += 1
            flat10_stake = actual_stake
            depth_stake = actual_stake
        else:
            capacity = money(binding_depth * market_depth_fraction)
            flat10_stake = _scaled_stake(actual_stake, min(target_stake, max_stake, capacity))
            edge_cap = edge_caps.get(edge_bucket)
            depth_ceiling = actual_stake if edge_cap is None else min(max_stake, edge_cap, capacity)
            depth_stake = _scaled_stake(actual_stake, depth_ceiling)
        flat10_pnl = money(roi_per_dollar * flat10_stake)
        depth_pnl = money(roi_per_dollar * depth_stake)
        for stats in (total, by_depth[depth_bucket], by_edge[edge_bucket], by_pair[pair]):
            stats.add(actual_stake, actual_pnl, flat10_stake, flat10_pnl, depth_stake, depth_pnl)
        if flat10_stake > actual_stake or depth_stake > actual_stake:
            examples.append({
                "id": int(row["id"]),
                "event": _event_label(legs),
                "pair": pair,
                "depth_bucket": depth_bucket,
                "edge_bucket": edge_bucket,
                "binding_depth": None if binding_depth is None else float(binding_depth),
                "edge_points": None if edge is None else float(edge),
                "actual_stake": float(money(actual_stake)),
                "actual_pnl": float(money(actual_pnl)),
                "flat10_stake": float(money(flat10_stake)),
                "flat10_pnl": float(flat10_pnl),
                "depth_stake": float(money(depth_stake)),
                "depth_pnl": float(depth_pnl),
                "depth_lift": float(money(depth_pnl - actual_pnl)),
            })
    examples.sort(key=lambda row: (row["depth_stake"], row["depth_lift"]), reverse=True)
    return {
        "rule": "Hypothetical only: settled staked ARBs, same observed per-dollar result, larger stake only when recorded depth allowed it.",
        "target_stake": float(money(target_stake)),
        "market_depth_fraction": float(market_depth_fraction),
        "max_stake": float(max_stake),
        "no_depth_n": no_depth,
        "total": total.as_json(),
        "depth": _scale_stats_map(by_depth),
        "edge": _scale_stats_map(by_edge),
        "venue_pair": _scale_stats_map(by_pair),
        "examples": examples[:25],
    }


def recommend_arb_stake(legs: list[dict[str, Any]], row_edge_points: Any,
                        model: dict[str, Any]) -> dict[str, Any]:
    # G2: bin/cap exactly as the model was trained — read the resolved knobs back from it,
    # falling back to the module defaults for models built before they were persisted.
    depth_buckets = _parse_buckets(model.get("depth_buckets"), DEPTH_BUCKETS)
    edge_buckets = _parse_buckets(model.get("edge_buckets"), EDGE_BUCKETS)
    edge_caps = {k: _dec(v) for k, v in model.get("edge_caps", {}).items()} or EDGE_CAPS
    min_edge_to_scale = _dec(model.get("min_edge_to_scale", str(_DEFAULT_MIN_EDGE_TO_SCALE)))
    min_bucket_roi = _dec(model.get("min_bucket_roi", str(_DEFAULT_MIN_BUCKET_ROI)))
    binding_depth = binding_depth_usd(legs)
    edge = _maybe_dec(row_edge_points)
    if edge is None:
        edge = edge_points({}, legs)
    pair = venue_pair(legs)
    depth_bucket = _bucket(binding_depth, depth_buckets)
    edge_bucket = _bucket(edge, edge_buckets)
    max_stake = _dec(model.get("max_stake", "0"))
    reasons: list[str] = []
    stake = Decimal("1.00")

    if binding_depth is None:
        reasons.append("no depth on one or more legs")
        return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=False)
    if edge is None or edge < min_edge_to_scale:
        reasons.append("past bet-on ARBs below 0.5% edge did not justify scaling")
        return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=False)

    min_sample = int(model.get("min_sample", 20))
    pair_stats = _lookup(model, "venue_pair", pair)
    depth_stats = _lookup(model, "depth", depth_bucket)
    edge_stats = _lookup(model, "edge", edge_bucket)
    for label, stats in (("venue pair", pair_stats), ("depth bucket", depth_stats), ("edge bucket", edge_stats)):
        if stats["n"] < min_sample:
            reasons.append(f"{label} has too few settled staked ARBs")
        elif Decimal(str(stats["roi"])) < min_bucket_roi:
            reasons.append(f"{label} realized ROI below 1%")
    if reasons:
        return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=False)

    edge_cap = edge_caps.get(edge_bucket)
    if edge_cap is None:
        reasons.append("edge bucket has no scale cap")
        return _recommendation(stake, binding_depth, edge, pair, depth_bucket, edge_bucket, reasons, scalable=False)

    depth_stake = binding_depth * Decimal(str(model.get("market_depth_fraction", _DEFAULT_MARKET_DEPTH_FRACTION)))
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


def _scale_stats_map(stats: dict[str, _ScaleStats]) -> dict[str, dict[str, Any]]:
    return {key: value.as_json() for key, value in sorted(stats.items())}


def _scaled_stake(actual_stake: Decimal, ceiling: Decimal) -> Decimal:
    return money(max(actual_stake, ceiling))


def _event_label(legs: list[dict[str, Any]]) -> str:
    first = legs[0] if legs else {}
    return str(first.get("event_key") or first.get("market_slug") or first.get("outcome_label") or "unknown event")


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
