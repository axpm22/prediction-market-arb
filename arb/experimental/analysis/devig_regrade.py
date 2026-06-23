"""Lever A.3 — de-vig regrade (BACKLOG_ROADMAP Wave 0, offline & read-only).

The payoff of persisting per-book probabilities: re-grade every stored EV flag under
proportional / power / shin de-vig and report how much the edge sign and magnitude move.
This GATES EV#2 (book-sharpness weighting) — if proportional and Shin agree within noise,
sharpness-weighting is low priority.

Selection caveat (stated loudly): flags were ORIGINALLY selected under proportional de-vig
>= ev_threshold, so this is an in-sample re-grade of an already-filtered set, not a full
replay from raw quotes. A full replay is the follow-up. Nothing here writes to the DB.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean, pstdev
from types import SimpleNamespace
from typing import Any

from arb.pricing import consensus_outcome_key, consensus_probabilities
from arb.store import Store

METHODS = ("proportional", "power", "shin")


@dataclass(frozen=True)
class MethodRegrade:
    method: str
    n: int
    mean_edge: float
    std_edge: float
    n_positive: int
    sign_flips_vs_proportional: int   # flags whose edge sign differs from proportional


def regrade_flags(flags: list[dict[str, Any]],
                  consensus_by_method: dict[str, dict[tuple[str, str], Decimal]]) -> list[MethodRegrade]:
    """Pure core: per method, edge = p_hat_method - flag_cost for each flag whose
    (event, outcome) has a consensus under that method. `flags` rows carry event_key,
    outcome_key, cost."""
    # per-flag edge under each method, keyed by flag index so we can compare sign flips.
    edges: dict[str, dict[int, float]] = {m: {} for m in consensus_by_method}
    for i, flag in enumerate(flags):
        key = (flag["event_key"], flag["outcome_key"])
        for method, consensus in consensus_by_method.items():
            p_hat = consensus.get(key)
            if p_hat is None:
                continue
            edges[method][i] = float(p_hat) - float(flag["cost"])
    base = edges.get("proportional", {})
    out: list[MethodRegrade] = []
    for method, per_flag in edges.items():
        vals = list(per_flag.values())
        if not vals:
            out.append(MethodRegrade(method, 0, 0.0, 0.0, 0, 0))
            continue
        flips = sum(
            1 for i, e in per_flag.items()
            if i in base and (e > 0) != (base[i] > 0)
        )
        out.append(MethodRegrade(
            method=method, n=len(vals),
            mean_edge=round(fmean(vals), 6),
            std_edge=round(pstdev(vals), 6) if len(vals) > 1 else 0.0,
            n_positive=sum(1 for e in vals if e > 0),
            sign_flips_vs_proportional=flips,
        ))
    return out


def regrade(store: Store, min_books: int, min_books_totals: int,
            methods: tuple[str, ...] = METHODS) -> list[MethodRegrade]:
    quotes = _load_oddsapi_quote_shims(store)
    consensus_by_method = {
        m: _consensus_all_events(quotes, min_books, min_books_totals, m) for m in methods
    }
    flags = _load_ev_flags(store)
    return regrade_flags(flags, consensus_by_method)


def _consensus_all_events(quotes: list[Any], min_books: int, min_books_totals: int,
                          method: str) -> dict[tuple[str, str], Decimal]:
    by_event: dict[str, list[Any]] = {}
    for q in quotes:
        by_event.setdefault(q.event_key, []).append(q)
    out: dict[tuple[str, str], Decimal] = {}
    for event_key, eq in by_event.items():
        for outcome, p in consensus_probabilities(eq, min_books, min_books_totals, method=method).items():
            out[(event_key, outcome)] = p
    return out


def _load_oddsapi_quote_shims(store: Store) -> list[Any]:
    with store.connect() as con:
        rows = con.execute(
            """SELECT event_key, book, market_type, line, outcome, raw_price
                 FROM quotes WHERE venue='oddsapi' AND raw_price IS NOT NULL"""
        ).fetchall()
    shims = []
    for r in rows:
        try:
            raw = Decimal(str(r["raw_price"]))
        except (TypeError, ValueError):
            continue
        shims.append(SimpleNamespace(
            venue="oddsapi", event_key=r["event_key"], book=r["book"],
            market_type=r["market_type"], outcome_label=r["outcome"],
            line=None if r["line"] in (None, "") else Decimal(str(r["line"])),
            raw_price=raw,
        ))
    return shims


def _load_ev_flags(store: Store) -> list[dict[str, Any]]:
    with store.connect() as con:
        rows = con.execute(
            "SELECT legs_json, optimistic_cost FROM flat_bets WHERE kind='EV' AND optimistic_cost IS NOT NULL"
        ).fetchall()
    flags: list[dict[str, Any]] = []
    for r in rows:
        try:
            leg = json.loads(r["legs_json"] or "[]")[0]
            cost = float(r["optimistic_cost"])
        except (IndexError, TypeError, ValueError):
            continue
        line = leg.get("line")
        outcome_key = consensus_outcome_key(
            str(leg.get("market_type", "WINNER")),
            None if line in (None, "") else Decimal(str(line)),
            str(leg.get("outcome_label", "")),
        )
        flags.append({"event_key": str(leg.get("event_key", "")), "outcome_key": outcome_key, "cost": cost})
    return flags


def format_regrade(results: list[MethodRegrade]) -> str:
    lines = [
        "De-vig regrade (IN-SAMPLE: flags were selected under proportional >= ev_threshold).",
        f"{'METHOD':>13} {'N':>6} {'MEAN_EDGE':>10} {'STD':>9} {'N_POS':>6} {'SIGN_FLIPS':>11}",
    ]
    for r in results:
        lines.append(f"{r.method:>13} {r.n:>6} {r.mean_edge:>+10.5f} {r.std_edge:>9.5f} "
                     f"{r.n_positive:>6} {r.sign_flips_vs_proportional:>11}")
    return "\n".join(lines)
