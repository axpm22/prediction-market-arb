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


def _mean(values: list[Decimal]) -> Decimal | None:
    return (sum(values, Decimal("0")) / len(values)).quantize(Decimal("0.000001")) if values else None


def _leg_markouts_for_bets(store) -> list[dict]:
    """For every EV/ARB bet leg (annotated rows excluded), the same-outcome mid at each horizon
    from the persisted quotes. Read-only; safe on a `mode=ro` archive. mid is None when no quote
    lands in the horizon's tolerance window (the gap is recorded, never fetched specially)."""
    import json
    from arb.experimental.annotations import annotated_ids
    with store.connect() as con:
        excluded = annotated_ids(con)
        bets = [dict(r) for r in con.execute(
            "SELECT id, placed_at, legs_json FROM flat_bets "
            "WHERE kind IN ('EV','ARB') AND placed_at IS NOT NULL")]
    out: list[dict] = []
    for bet in bets:
        if int(bet["id"]) in excluded or not bet["placed_at"]:
            continue
        entry = _parse(bet["placed_at"])
        for leg in json.loads(bet["legs_json"] or "[]"):
            ek, venue = leg.get("event_key"), leg.get("venue")
            outcome = leg.get("outcome_label") or leg.get("outcome")
            entry_cost = leg.get("cost")
            if not (ek and venue and outcome and entry_cost not in (None, "")):
                continue
            entry_dec = Decimal(str(entry_cost))
            for h in HORIZONS:
                m = markout_at(store, str(ek), str(venue), str(outcome), entry, h)
                out.append({"bet_id": int(bet["id"]), "venue": str(venue), "horizon": h,
                            "entry": entry_dec, "mid": None if m is None else m["mid"],
                            "ask": None if m is None else m["ask"], "bid": None if m is None else m["bid"]})
    return out


def persist_markouts(store) -> int:
    """Persist hook: UPSERT each bet leg's markout samples into `markouts` (operational table —
    UPDATE allowed). Uses only quotes the loops already fetched (no new HTTP). A horizon with no
    quote in-window is stored NULL. Idempotent. Never call on a read-only DB (use markout_summary).
    Returns rows written."""
    samples = _leg_markouts_for_bets(store)  # read phase (own connections)
    with store.connect() as con:             # write phase
        for s in samples:
            con.execute(
                """INSERT INTO markouts(bet_id, horizon_secs, mid, ask, bid, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bet_id, horizon_secs) DO UPDATE
                     SET mid=excluded.mid, ask=excluded.ask, bid=excluded.bid,
                         recorded_at=excluded.recorded_at""",
                (s["bet_id"], s["horizon"],
                 None if s["mid"] is None else str(s["mid"]),
                 None if s["ask"] is None else str(s["ask"]),
                 None if s["bid"] is None else str(s["bid"]),
                 datetime.now(timezone.utc).isoformat()))
    return len(samples)


def markout_summary(store) -> dict[int, dict]:
    """Per-horizon markout stats across all bet legs (read-only; works on the archive). markout =
    mid(H) − entry, signed; persistently negative = adverse selection. {horizon: {n, median, mean}}."""
    samples = _leg_markouts_for_bets(store)
    by_h: dict[int, list[Decimal]] = {h: [] for h in HORIZONS}
    for s in samples:
        if s["mid"] is not None:
            by_h[s["horizon"]].append(markout_cents(s["entry"], s["mid"]))
    return {h: {"n": len(v), "median": median(v), "mean": _mean(v)} for h, v in by_h.items()}


def format_markout_summary(summary: dict[int, dict]) -> str:
    label = {30: "+30s", 120: "+2m", 600: "+10m", 1800: "+30m"}
    lines = ["Markout (mid at horizon − entry; negative = adverse selection):",
             f"  {'horizon':>8}{'n':>7}{'median':>12}{'mean':>12}"]
    for h in HORIZONS:
        st = summary.get(h, {})
        med = "—" if st.get("median") is None else f"{st['median']:+f}"
        mean = "—" if st.get("mean") is None else f"{st['mean']:+f}"
        lines.append(f"  {label[h]:>8}{st.get('n', 0):>7}{med:>12}{mean:>12}")
    return "\n".join(lines)
