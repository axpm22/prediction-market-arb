"""Read-time capacity projection: what each locked flag WOULD have supported if
sized by walking the book instead of the touch.

This is deliberately NOT arb/capacity.py. That module is the sanctioned,
gated report (§18.4): it refuses to speak below `min_ladder_fraction` and
covers trusted+clean rows only, because inflated depth claims were half of a
previous "$72,209 fantasy". Nothing here relaxes that gate, changes detection,
or touches `arb_size_by_ladder`. It exists so the multiplier can be MEASURED
before anyone decides whether to enable ladder sizing for real.

The difference that makes it useful: `sizing.arb_projection` returns None when
any leg lacks a ladder, and Kalshi ladders are never fetched (0 of 3.9M stored
quotes), so every Kalshi-involving flag projects as nothing. Here a leg without
a ladder is simply capped at its touch depth and *reported as the binding
constraint*, which is what turns "we can't size this" into "here is the one
venue whose book we are not reading".

Every number is a projection over stored quotes. No fill is claimed.
"""
from __future__ import annotations

import json
from decimal import Decimal

from arb.models import LOCKED_KINDS, q6
from arb.sizing import parse_ladder, q_star, vwap_cost

# why a position could not be sized larger — the whole point of the module
BIND_TOUCH = "touch cap (no ladder stored)"
BIND_LADDER = "ladder exhausted"
BIND_EDGE = "edge died"
BIND_BANKROLL = "bankroll cap"


def _leg_quote(store, poll_id, event_key, leg) -> dict | None:
    """The stored quote row behind one leg. Same lookup as capacity._leg_quote,
    including its complement-map fallback: quotes persist PRE-map, so a mapped
    YES leg's source row is the other outcome's NO."""
    sql = """SELECT ladder_json, depth_usd, cost FROM quotes
             WHERE poll_id=? AND venue=? AND event_key=? AND market_type=?
               AND (line IS ? OR line=?) AND side=? AND {outcome} LIMIT 1"""
    args = (poll_id, leg["venue"], event_key, leg["market_type"],
            leg["line"], leg["line"])
    rows = store.query(sql.format(outcome="outcome=?"),
                       (*args, leg["side"], leg["outcome"]))
    if not rows and leg["side"] == "YES":
        other = {"OVER": "UNDER", "UNDER": "OVER"}.get(leg["outcome"])
        clause, arg = ("outcome=?", other) if other else ("outcome!=?", leg["outcome"])
        rows = store.query(sql.format(outcome=clause), (*args, "NO", arg))
    return dict(rows[0]) if rows else None


def project_position(cfg, legs: list[dict], quotes: list[dict]) -> dict | None:
    """Projected contracts for one locked position under THREE independent
    ceilings, reported separately because they are different kinds of thing:

      bankroll  a POLICY choice (max_kelly_fraction_of_bankroll x bankroll)
      touch     a DATA limit — all we know when only top-of-book is stored
      ladder    a DATA limit — q*, the size whose VWAP slippage stays inside
                slippage.max_edge_fraction of net edge
      zero_edge the theoretical ceiling: walk until the edge is exactly gone

    Conflating them is how the first cut of this module reported ladder sizing
    as WORSE than touch sizing — the bankroll cap was applied to one and not
    the other. Keeping them apart is what lets the caller ask the only question
    that matters: at what bankroll does depth, rather than policy, start to bind?
    """
    costs = [Decimal(lg["cost"]) + Decimal(lg.get("fee_cost") or 0) for lg in legs]
    cost_sum = sum(costs)
    net_edge = 1 - cost_sum
    if net_edge <= 0 or any(q is None for q in quotes):
        return None
    ex, sl, sz = cfg["execution"], cfg["slippage"], cfg["sizing"]

    bankroll_c = sz["max_kelly_fraction_of_bankroll"] * cfg["bankroll_usd"] / cost_sum
    touch_caps, ladder_caps, zero_caps, ladders = [], [], [], []
    for lg, qrow in zip(legs, quotes):
        ladder = parse_ladder(qrow.get("ladder_json"))
        ladders.append(ladder)
        depth = Decimal(str(qrow["depth_usd"] or 0)) * ex["fillable_fraction"]
        price = Decimal(lg["cost"])
        touch = depth / price if price > 0 else Decimal(0)
        touch_caps.append(touch)
        if len(ladder) >= 2:
            ladder_caps.append(q_star(ladder, net_edge, sl["max_edge_fraction"]))
            zero_caps.append(_zero_edge_contracts(ladder, price + net_edge))
        else:
            # no book stored: the touch is genuinely all we know
            ladder_caps.append(touch)
            zero_caps.append(touch)
    out = {
        "net_edge": q6(net_edge),
        "bankroll_contracts": q6(bankroll_c),
        "touch_contracts": q6(min(touch_caps)),
        "ladder_contracts": q6(min(ladder_caps)),
        "zero_edge_contracts": q6(min(zero_caps)),
        "no_ladder_venues": [lg["venue"] for lg, l in zip(legs, ladders) if len(l) < 2],
    }
    for key in ("bankroll", "touch", "ladder", "zero_edge"):
        c = out[f"{key}_contracts"]
        out[f"{key}_pnl_usd"] = q6(c * net_edge)
        out[f"{key}_stake_usd"] = q6(c * cost_sum)
    binders = [(out["bankroll_contracts"], BIND_BANKROLL),
               (out["ladder_contracts"],
                BIND_TOUCH if out["no_ladder_venues"] else BIND_LADDER)]
    out["binder"] = min(binders, key=lambda b: b[0])[1]
    return out


def _zero_edge_contracts(ladder, max_vwap: Decimal) -> Decimal:
    """Largest size whose VWAP stays at or below `max_vwap` — i.e. walk the book
    until the edge is exactly consumed. The theoretical ceiling, not a target:
    it books zero profit at the margin and ignores every execution risk."""
    filled, cost = Decimal(0), Decimal(0)
    for price, size in ladder:
        if (cost + price * size) / (filled + size) <= max_vwap:
            filled += size
            cost += price * size
            continue
        if price <= max_vwap:
            continue
        return q6((price * filled - cost) / (price - max_vwap)) if filled else Decimal(0)
    return q6(filled)


def projection_frame(cfg, store, from_date: str, to_date: str | None = None) -> dict:
    """Aggregate the projection over every locked flag in a date range,
    deduped by position_key (V1's B2 bug counted the same position 4.5x).

    Unlike capacity_frame this does NOT filter to trusted+clean rows — with
    `trusted_pairs` empty that filter yields the empty set, which is correct
    for a sanctioned report and useless for measuring a multiplier. Lane and
    trust are returned per row so the caller can split them."""
    from arb.metrics import day_bounds
    start, _ = day_bounds(cfg, from_date)
    _, end = day_bounds(cfg, to_date or from_date)
    ks = ",".join("?" * len(LOCKED_KINDS))
    rows = store.query(
        f"SELECT * FROM flat_bets WHERE created_at >= ? AND created_at < ? "
        f"AND kind IN ({ks}) ORDER BY id", (start, end, *LOCKED_KINDS))
    seen: set[str] = set()
    out = {"n_flags": 0, "n_projected": 0, "positions": [], "binders": {},
           "pnl": {k: Decimal(0) for k in ("bankroll","touch","ladder","zero_edge")},
           "stake": {k: Decimal(0) for k in ("bankroll","touch","ladder","zero_edge")}}
    for r in rows:
        b = dict(r)
        if b["position_key"] in seen:
            continue
        seen.add(b["position_key"])
        out["n_flags"] += 1
        legs = json.loads(b["legs_json"])
        quotes = [_leg_quote(store, b["poll_id"], b["event_key"], lg) for lg in legs]
        proj = project_position(cfg, legs, quotes)
        if not proj:
            continue
        out["n_projected"] += 1
        proj.update(position_key=b["position_key"], kind=b["kind"], lane=b["lane"],
                    trusted=b["trusted"], venue_pair=b["venue_pair"])
        out["positions"].append(proj)
        out["binders"][proj["binder"]] = out["binders"].get(proj["binder"], 0) + 1
        for k in out["pnl"]:
            out["pnl"][k] += proj[f"{k}_pnl_usd"]
            out["stake"][k] += proj[f"{k}_stake_usd"]
    for k in out["pnl"]:
        out["pnl"][k] = q6(out["pnl"][k]); out["stake"][k] = q6(out["stake"][k])
    return out
