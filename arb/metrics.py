"""metrics.py — ONE place computing every displayed number (SPEC §14.1, bug I7).
Derives pnl_confirmed / result / settled_at at read time from bet_events; no
derived value is ever stored back (rule 9). The exit engine (§10.8) lives here:
read-time counterfactual columns, pure functions of stored quotes."""
from __future__ import annotations

import json
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from arb.models import LOCKED_KINDS, q6
from arb.venues.fees import leg_fee_cost


def day_bounds(cfg, day: str) -> tuple[str, str]:
    """[start, end) UTC-ISO bounds for the config-timezone (ET) calendar day
    `day` (YYYY-MM-DD). Daily boundaries are ET/EDT (config.yaml), but stored
    timestamps are UTC ISO; a half-open range on them honors the timezone AND
    lets SQLite use the created_at/fetched_at indexes instead of a per-row
    date() function scan (H5). Uses next-ET-midnight (not +24h) so it is correct
    across DST. Assumes stored timestamps are UTC ISO with a +00:00 offset, as
    every writer here produces via aware-datetime .isoformat()."""
    tz = ZoneInfo(str(cfg["timezone"]))
    d = _date.fromisoformat(day)
    nd = d + timedelta(days=1)
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)
    end = datetime(nd.year, nd.month, nd.day, tzinfo=tz).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


def _events(store, bet_id: int) -> list[dict]:
    return [dict(r) for r in store.query(
        "SELECT * FROM bet_events WHERE bet_id=? ORDER BY id", (bet_id,))]


def _annotated(store, bet_id: int, kinds=("settle-tripwire", "INVALID_AFTER_FINAL")):
    rows = store.query(
        "SELECT kind FROM annotations WHERE target_kind='bet' AND target_id=?",
        (str(bet_id),))
    return [r["kind"] for r in rows if r["kind"] in kinds]


def bet_effective_cost(bet: dict) -> Decimal:
    legs = json.loads(bet["legs_json"])
    return sum(Decimal(l["cost"]) + Decimal(l.get("fee_cost") or 0) for l in legs)


def bet_pnl_optimistic(store, bet: dict) -> Decimal | None:
    """Locked kinds: stored at entry. Resolution-settled: derived from the
    terminal settled event + entry costs. Pending -> None."""
    if bet.get("pnl_optimistic") is not None:
        return Decimal(str(bet["pnl_optimistic"]))
    settled = [e for e in _events(store, bet["id"]) if e["kind"] == "settled"]
    if not settled:
        return None
    payload = json.loads(settled[0]["payload_json"])
    stake = Decimal(str(bet["stake_usd"]))
    legs = json.loads(bet["legs_json"])
    c = bet_effective_cost(bet)
    results = payload.get("per_leg") or [payload["result"]]
    if "fraction" in payload:  # PARTIAL(f): pnl = stake × (f/cost − 1)  (v3.4)
        return q6(stake * (Decimal(payload["fraction"]) / c - 1))
    if any(r not in ("WON", "LOST", "VOID") for r in results):
        return None  # a leg still unresolved -> whole bet pending (H2)
    # Per-leg payoff sum (SPEC §7.2 equal-contracts identity), which subsumes the
    # single-leg case. Multi-leg (NEGRISK baskets) previously fell through to None.
    contracts = stake / c  # equal counts per leg
    total = Decimal(0)
    for leg, r in zip(legs, results):
        ci = Decimal(leg["cost"]) + Decimal(leg.get("fee_cost") or 0)
        if r == "WON":
            total += contracts * (1 - ci)
        elif r == "LOST":
            total -= contracts * ci
        # VOID leg: contributes 0
    return q6(total)


def bet_confirmed(store, bet: dict) -> tuple[bool, Decimal | None]:
    """(confirmed?, pnl_confirmed). Missing/in-play confirm = MISSED; the miss
    enters the confirmed cell as $0 (§10.3 — the bet was never made)."""
    evs = _events(store, bet["id"])
    conf = [e for e in evs if e["kind"] == "confirmed"]
    if not conf:
        return False, None
    payload = json.loads(conf[0]["payload_json"])
    cost_sum = Decimal(payload["cost_sum"])
    stake = Decimal(str(bet["stake_usd"]))
    if bet["kind"] in LOCKED_KINDS:
        # payout per unit basket: 1 unless stamped at entry (NEGRISK NO-baskets
        # pay N-1; assuming 1 was review-H3's wrong-money formula)
        payout = Decimal(str(bet["payout"])) \
            if "payout" in bet.keys() and bet["payout"] is not None else Decimal(1)
        return True, q6(stake * (payout / cost_sum - 1))
    # resolution-settled: same result basis, confirmed entry price. The result
    # class comes from the settled payload, NEVER the sign of the pnl — a
    # profitable PARTIAL (fraction > cost) is not a WIN.
    opt = bet_pnl_optimistic(store, bet)
    if opt is None:
        return True, None  # confirmed but pending resolution
    settled = [e for e in evs if e["kind"] == "settled"]
    payload = json.loads(settled[0]["payload_json"])
    results = payload.get("per_leg") or [payload["result"]]
    if "fraction" in payload or len(results) > 1:
        # PARTIAL(f): entry price can't change the payoff; multi-leg lacks
        # per-leg confirmed prices to recompute on -> passthrough
        return True, opt
    if results[0] == "WON":  # recompute the win payout on the confirmed price
        return True, q6(stake * (1 - cost_sum) / cost_sum)
    if results[0] == "LOST":  # full stake lost regardless of entry price
        return True, -stake
    return True, opt  # VOID: entry price can't change the payoff


def pnl_confirmed_cell(rows: list[dict]) -> tuple[Decimal, int]:
    """Aggregate a confirmed-P&L cell: missed counts $0 AND is counted (bug I8)."""
    total, missed = Decimal(0), 0
    for r in rows:
        if not r.get("confirmed"):
            missed += 1
        elif r.get("pnl_if_confirmed") is not None:
            total += Decimal(str(r["pnl_if_confirmed"]))
    return total, missed


def pnl_markout(cfg, bet: dict, marks: list[dict]) -> Decimal | None:
    """One-sided adverse drift (v3.4 sign): pnl_optimistic + Σ_legs min(0, D_h) ×
    contracts; +30s horizon, else earliest covered PREGAME horizon; in_play marks
    excluded. Favorable drift never credited."""
    if bet.get("pnl_optimistic") is None:
        return None
    legs = json.loads(bet["legs_json"])
    costs = [Decimal(l["cost"]) for l in legs]
    s = sum(costs)
    contracts = Decimal(str(bet["stake_usd"])) / s  # equal counts per leg (§7.2)
    total = Decimal(str(bet["pnl_optimistic"]))
    for i, leg in enumerate(legs):
        if leg.get("mid0") is None:
            continue
        leg_marks = sorted((m for m in marks
                            if m["leg_index"] == i and not m["in_play"]),
                           key=lambda m: m["horizon_secs"])
        if not leg_marks:
            continue
        drift = Decimal(str(leg_marks[0]["mark_cost"])) - Decimal(leg["mid0"])
        total += min(Decimal(0), drift) * contracts
    return q6(total)


def exit_policy_pnls(cfg, bet: dict, quote_series: list[dict],
                     category: str = "sports") -> dict:
    """§10.8 SELL engine, read-time. quote_series: [{bid, at, mid?}] ascending.
    Sell basis binding: exit_value = bid − taker fee on the exit trade. (H10
    dropped the never-computed "hold" key — hold-to-resolution pays P0, which
    bet_pnl_optimistic already computes; a perpetually-None column is dead.)"""
    legs = json.loads(bet["legs_json"])
    if len(legs) != 1:
        return {"take_profit": None, "trailing": None}
    leg = legs[0]
    c = Decimal(leg["cost"]) + Decimal(leg.get("fee_cost") or 0)
    stake = Decimal(str(bet.get("stake_usd") or 1))
    contracts = stake / c
    flagged = Decimal(str(bet["edge_points"]))
    ex = cfg["exit"]
    out = {"take_profit": None, "trailing": None}
    peak = None
    armed = False
    for pt in quote_series:
        bid = Decimal(str(pt["bid"]))
        exit_value = bid - leg_fee_cost(cfg, leg["venue"], category, bid)
        gain = exit_value - c
        if out["take_profit"] is None and gain >= ex["capture_fraction"] * flagged:
            out["take_profit"] = q6(contracts * gain)
        mid = Decimal(str(pt.get("mid", bid)))
        unrealized = contracts * (mid - c)
        peak = unrealized if peak is None else max(peak, unrealized)
        if not armed and peak >= ex["min_peak_edge_fraction"] * flagged * contracts:
            armed = True
        if armed and out["trailing"] is None and peak > 0 \
                and unrealized <= peak * (1 - ex["trail_fraction"]):
            out["trailing"] = q6(contracts * gain)
    return out
