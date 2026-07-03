"""MODEL_FOUNDATIONS Phase C — make paper fills earn their optimism.

Adverse selection is measurable without trading: the conditional drift of the same outcome's
price after you would have been filled. If flags are informationless the post-flag path is
symmetric; if books pick us off, quotes we "took" drift against us. This reads the persisted
`quotes` (already sampled densely by the watch + hot loops), computes the markout at +30s / +2m /
+10m / +30m for each bet leg, and derives pnl_markout using the +30s re-quote as entry ("you're
never faster than 30 seconds"). Also the zombie-order guard: a quote is only fillable once seen
at the same price in >=2 consecutive polls or flash-verified — single-appearance quotes flicker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

HORIZONS = (30, 120, 600, 1800)  # seconds: +30s, +2m, +10m, +30m


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _quote_mid(cost: str, best_bid: str | None) -> Decimal:
    ask = Decimal(str(cost))
    if best_bid in (None, ""):
        return ask
    bid = Decimal(str(best_bid))
    return (bid + ask) / 2 if Decimal("0") < bid < ask else ask


def markout_at(store, event_key: str, venue: str, outcome: str, entry: datetime,
               horizon_secs: int, tol_secs: int = 90) -> dict | None:
    """The same outcome's quote nearest to entry+horizon (within tol) on the same venue.
    Returns {mid, ask, bid} or None if no quote lands in the window."""
    target = entry + timedelta(seconds=horizon_secs)
    lo, hi = (target - timedelta(seconds=tol_secs)).isoformat(), (target + timedelta(seconds=tol_secs)).isoformat()
    with store.connect() as con:
        rows = list(con.execute(
            "SELECT cost, best_bid, fetched_at FROM quotes "
            "WHERE event_key=? AND venue=? AND outcome=? AND fetched_at BETWEEN ? AND ?",
            (event_key, venue, outcome, lo, hi)))
    if not rows:
        return None
    best = min(rows, key=lambda r: abs((_parse(r["fetched_at"]) - target).total_seconds()))
    return {"mid": _quote_mid(best["cost"], best["best_bid"]),
            "ask": Decimal(str(best["cost"])),
            "bid": None if best["best_bid"] in (None, "") else Decimal(str(best["best_bid"]))}


def markout_cents(entry_cost: Decimal, mid_at_horizon: Decimal) -> Decimal:
    """Signed markout in $ per $1 payout: mid(H) - entry. Persistently negative = adverse
    selection (you bought at `entry` and fair value fell below it)."""
    return mid_at_horizon - entry_cost


def pnl_markout(mid_30s: Decimal, result: str, stake: Decimal) -> Decimal | None:
    """Third P&L basis: enter at the +30s re-quote instead of the flag-time quote (harsher, more
    honest than `confirmed`). WON pays (1-entry)/entry * stake; LOST pays -stake; VOID 0."""
    if result == "VOID":
        return Decimal("0")
    if mid_30s <= 0 or mid_30s >= 1:
        return None
    if result == "WON":
        return ((Decimal("1") - mid_30s) / mid_30s * stake).quantize(Decimal("0.000001"))
    if result == "LOST":
        return -stake
    return None


def zombie_fillable(appearances: list[dict], flash_verified: bool = False) -> tuple[bool, str | None]:
    """A quote counts as fillable only if seen at the SAME price in >=2 consecutive polls, or
    flash-verified once. Otherwise it's a single-appearance flicker: recorded, never booked.
    `appearances` is the quote's observations in poll order: [{poll_id, cost}, ...]."""
    if flash_verified:
        return True, None
    prices_in_order = [str(a["cost"]) for a in sorted(appearances, key=lambda a: a["poll_id"])]
    for earlier, later in zip(prices_in_order, prices_in_order[1:]):
        if earlier == later:
            return True, None
    return False, "flicker"


def median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
