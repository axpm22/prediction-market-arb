"""ATTIC — code that is not on any live path, kept for reference.

Nothing here is imported by `arb/`. It was moved out on 2026-07-28 after a
reference sweep found zero call sites in `arb/` or `tests/`. It is parked
rather than deleted because most of it is a probe we may want back the moment
a lane that needs it switches on.

To restore an item: move the function back into the module named in its
comment and re-add its import. Nothing else has to change — none of these had
callers to rewire.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# from arb/metrics.py — the SELL engine (SPEC 10.8).
# Never called from anywhere, and it returns immediately unless the bet has
# exactly ONE leg, so it has never evaluated a locked arb. Measured 2026-07-28:
# unwinding a 2-leg cover costs a median 9c of round-trip spread against a
# 0.5c edge, and a covering pair is delta-neutral so waiting cannot help.
# Restore if the single-leg EV lane is ever re-opened.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# from arb/venues/kalshi.py — fetch_event / fetch_trades / refresh_series_cache.
# fetch_event was for negRisk mutually-exclusive checks, but arb/negrisk.py is
# pure math over dicts and imports nothing from the venue. fetch_trades is the
# tape probe; the bench pulls tape via arb/kalshi_history.pull_trades instead.
# ---------------------------------------------------------------------------
def fetch_event(cfg, event_ticker: str) -> dict:
    """Event object; `mutually_exclusive` feeds the negRisk proof condition (a)."""
    return get_json(f"{BASE}/events/{event_ticker}", cfg).get("event", {})


def fetch_trades(cfg, ticker: str, limit: int = 100) -> list[dict]:
    return get_json(f"{BASE}/markets/trades?ticker={ticker}&limit={limit}", cfg).get("trades", [])


def refresh_series_cache(cfg) -> None:
    data = get_json(f"{BASE}/series?category=Sports", cfg)
    SERIES_CACHE.parent.mkdir(exist_ok=True)
    SERIES_CACHE.write_text(json.dumps(data, indent=1))


# ---------------------------------------------------------------------------
# from arb/venues/sx.py — fetch_market_status.
# SX settled-status semantics are still unverified (data/verified_facts.md),
# so reconcile._sx returns None unconditionally and never calls this.
# ---------------------------------------------------------------------------
def fetch_market_status(cfg, market_hashes: list[str]) -> list[dict]:
    """GET /markets/find — settled-status semantics UNVERIFIED (verified_facts.md);
    reconcile treats anything but a verified settled status as None."""
    hashes = ",".join(market_hashes)
    return get_json(f"{BASE}/markets/find?marketHashes={hashes}", cfg).get("data", [])


# ---------------------------------------------------------------------------
# from arb/venues/oddsapi.py — fetch_scores.
# Settlement resolves MLB through the free MLB Stats API instead, so this
# would spend Odds API credits for data we already get for nothing.
# ---------------------------------------------------------------------------
def fetch_scores(cfg, sport_key: str) -> list[dict]:
    scores, _ = _get(cfg, f"/sports/{sport_key}/scores/",
                     {"daysFrom": int(cfg["settle_days_from"])})
    return scores
