"""Single-venue exhaustive-outcome baskets (SPEC §2, §8.4): Polymarket negRisk
events and Kalshi mutually-exclusive event groups, scanned on BOTH sides.

The exhaustiveness proof (conditions a–d) gates everything: a non-exhaustive
group is a short position in every missing outcome. Failing baskets are REVIEW
`unproven-exhaustive`, never clean, never dropped."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from arb.models import q6
from arb.venues.fees import leg_fee_cost


def exhaustiveness_proof(cfg, outcomes: list[dict], venue_flag: bool
                         ) -> tuple[str, str]:
    """-> ('proven'|'unproven-exhaustive', reason). outcomes: dicts with
    yes_ask/yes_bid (Decimal|None)."""
    proof = cfg["negrisk"]["exhaustive_proof"]
    if proof["require_venue_flag"] and not venue_flag:
        return "unproven-exhaustive", "venue-flag-missing"
    if proof["require_two_sided_all_outcomes"]:
        for o in outcomes:
            if not o.get("yes_ask") or not o.get("yes_bid") \
                    or o["yes_ask"] >= 1 or o["yes_bid"] <= 0:
                return "unproven-exhaustive", f"two-sided-fail:{o['label']}"
    if any(o.get("yes_ask") is None or o.get("yes_bid") is None for o in outcomes):
        # mids need both sides regardless of the flag (M8: Decimal + None crash)
        return "unproven-exhaustive", "one-sided-outcome"
    mids = [(o["yes_ask"] + o["yes_bid"]) / 2 for o in outcomes]
    mids_sum = sum(mids)
    if not (proof["mids_sum_min"] <= mids_sum <= proof["mids_sum_max"]):
        return "unproven-exhaustive", f"mids-sum={q6(mids_sum)}"
    half_spread_sum = sum((o["yes_ask"] - o["yes_bid"]) / 2 for o in outcomes)
    if half_spread_sum > proof["max_half_spread_sum"]:
        return "unproven-exhaustive", f"half-spread-sum={q6(half_spread_sum)}"
    return "proven", "ok"


def _basket(cfg, venue, category, event_key, title, outcomes, side, proof_status,
            proof_reason) -> dict | None:
    n = len(outcomes)
    ask_key = "yes_ask" if side == "YES" else "no_ask"
    if any(o.get(ask_key) is None for o in outcomes):
        return None
    fees = [leg_fee_cost(cfg, venue, category, o[ask_key]) for o in outcomes]
    costs = [o[ask_key] + f for o, f in zip(outcomes, fees)]
    s = sum(costs)
    payout = Decimal(1) if side == "YES" else Decimal(n - 1)
    edge = 1 - s / payout   # edge_points per $1 payout (§7.1)
    if edge <= 0:
        return None         # break-even or worse must NOT flag (bug C0)
    roi = payout / s - 1
    status, notes = "OK", [proof_reason] if proof_status != "proven" else []
    if proof_status != "proven":
        status = "REVIEW"
    if edge < cfg["negrisk"]["min_edge"]:
        status, notes = "REVIEW", notes + [f"below-min-edge={q6(edge)}"]
    if roi > cfg["arb_stale_warn"]:
        status, notes = "REVIEW", notes + [f"stale-warn roi={q6(roi)}"]
    if any((o.get("depth_usd") or 0) < cfg["negrisk"]["min_executable_usd"]
           for o in outcomes):
        status, notes = "REVIEW", notes + ["thin-depth"]
    basket_sha = hashlib.sha1(
        "|".join(sorted(str(o["label"]) for o in outcomes)).encode()).hexdigest()
    return {
        "kind": "NEGRISK", "venue": venue, "category": category,
        "event_key": event_key, "title": title, "side": side, "n_outcomes": n,
        "edge_points": q6(edge), "roi": q6(roi), "cost_sum": q6(s),
        "payout": payout, "status": status, "proof_status": proof_status,
        "notes": "; ".join(notes),
        "fingerprint": hashlib.sha1(
            f"{event_key}|NEGRISK|{side}|{basket_sha}".encode()).hexdigest(),
        "position_key": f"{category}|{event_key}|BASKET|{basket_sha}",
        # consumer schema (capacity/sizing/tracker/settle all read these keys);
        # cost and fee split — fee-inclusive-only legs were the D1 trap
        "legs_json": json.dumps([{
            "venue": venue, "market_type": "WINNER", "line": None,
            "outcome": o["label"], "side": side,
            "cost": str(q6(o[ask_key])), "fee_cost": str(q6(fees[i]))}
            for i, o in enumerate(outcomes)]),
        "venue_pair": f"{venue}:self",
    }


def quote_rows(cfg, venue: str, category: str, event_key: str,
               outcomes: list[dict], now) -> list[dict]:
    """Every observed outcome side as a quotes row (rule 7 — the scan's books
    are backtest data whether or not a basket flags)."""
    rows = []
    for o in outcomes:
        for side, ask_key, bid_key in (("YES", "yes_ask", "yes_bid"),
                                       ("NO", "no_ask", "no_bid")):
            ask = o.get(ask_key)
            if ask is None:
                continue
            rows.append({
                "fetched_at": now.isoformat(), "event_key": event_key,
                "category": "nonsports", "sport": category, "venue": venue,
                "book": venue, "market_type": "WINNER", "line": None,
                "outcome": o["label"], "side": side, "raw_price": ask,
                "cost": ask, "fee_cost": leg_fee_cost(cfg, venue, category, ask),
                "depth_usd": o.get("depth_usd") if side == "YES" else None,
                "ladder_json": None, "live": False, "bid": o.get(bid_key),
                "volume_24h": o.get("volume_24h"), "start_utc": None,
                "start_grade": "absent", "venue_market_id": None})
    return rows


def scan_group(cfg, venue: str, category: str, event_key: str, title: str,
               outcomes: list[dict], venue_flag: bool) -> list[dict]:
    """Both sides of one exhaustive-outcome group (§2): YES basket (sum of YES
    asks < 1) and NO basket (sum of NO asks < N-1, N>=3)."""
    if not outcomes or len(outcomes) > int(cfg["negrisk"]["max_outcomes"]):
        return []
    proof_status, proof_reason = exhaustiveness_proof(cfg, outcomes, venue_flag)
    opps = []
    b = _basket(cfg, venue, category, event_key, title, outcomes, "YES",
                proof_status, proof_reason)
    if b:
        opps.append(b)
    if cfg["negrisk"]["scan_no_side"] and len(outcomes) >= 3:
        b = _basket(cfg, venue, category, event_key, title, outcomes, "NO",
                    proof_status, proof_reason)
        if b:
            opps.append(b)
    return opps
