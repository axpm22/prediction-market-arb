"""L12 — maker bracket from the Polymarket WS tape.

Completes what bench_markout.l7_maker could only half-do. L7 works from HOURLY
Kalshi candles and can only see the conservative side, which is why its own
note reads "a one-sided bracket bounds nothing". At WebSocket resolution both
sides are reachable, so this lane reports a real bracket.

FILLS COME FROM REAL TRADE PRINTS, never from book deltas. Task 1 verified
(2026-07-30) that Polymarket emits `last_trade_price` carrying price, size,
side and a transaction hash. That matters more than it sounds: size leaving a
price level is NOT evidence of a trade — it is equally a CANCELLATION, and
counting cancellations as fills inflates every maker number upward with no way
to bound the error. A print is unambiguous.

The cost of that honesty is sparsity: 26 prints against 13,560 price_change
frames in the verification probe (~0.2%). A maker tape therefore has to be
LONG rather than wide, and `n` is the thing to watch before reading any mean.
"""
from __future__ import annotations

import json
from decimal import Decimal

from arb.bench import LaneResult, verdict_for
from arb.maker import MakerSim, bracket, markout
from arb.venues.fees import leg_fee_cost


def _best_bid(frame: dict) -> tuple[Decimal, Decimal] | None:
    """(price, size) at the best bid of a `book` frame, or None.

    Sentinel prices (0, 1) and zero sizes are the empty/resolved-book pattern
    already known to manufacture fake covers elsewhere in this codebase.
    """
    best = None
    for lv in frame.get("bids") or []:
        try:
            p, s = Decimal(str(lv["price"])), Decimal(str(lv["size"]))
        except (KeyError, TypeError, ArithmeticError):
            continue
        if not (0 < p < 1) or s < 0:
            continue
        if best is None or p > best[0]:
            best = (p, s)
    return best


def _maker_fee(cfg, price: Decimal) -> Decimal:
    return leg_fee_cost(cfg, "polymarket", "politics", price,
                        Decimal(1), "paper", "maker")


def replay(cfg, tape_con) -> dict:
    """Walk the tape once: post at the touch, fill on prints, mark out later."""
    horizons = sorted(int(h) for h in cfg["maker"]["markout_horizons_secs"])
    sim = MakerSim(fee_rate=Decimal(0))     # fee is charged per fill at markout
    fills: list[dict] = []
    bids: dict[str, list[tuple[float, Decimal]]] = {}   # token -> (t, best_bid)
    prints = 0
    tokens: set[str] = set()

    for r in tape_con.execute(
            "SELECT received_at, asset_id, event_type, raw_json FROM tape_frames "
            "ORDER BY received_at, id"):
        tok = r["asset_id"]
        if not tok:
            continue
        at = float(r["received_at"])
        frame = json.loads(r["raw_json"])
        kind = r["event_type"]
        tokens.add(tok)

        if kind == "book":
            touch = _best_bid(frame)
            if touch is None:
                continue
            price, size = touch
            bids.setdefault(tok, []).append((at, price))
            if tok not in sim.orders:
                # join the queue at the BACK: everything resting is ahead of us
                sim.post(tok, price, queue_ahead=size, posted_at=at)

        elif kind == "price_change":
            for ch in frame.get("price_changes") or []:
                if str(ch.get("asset_id")) != tok:
                    continue
                try:
                    bb = Decimal(str(ch.get("best_bid")))
                except (TypeError, ArithmeticError):
                    continue
                if 0 < bb < 1:
                    bids.setdefault(tok, []).append((at, bb))

        elif kind == "last_trade_price":
            try:
                price = Decimal(str(frame.get("price")))
                size = Decimal(str(frame.get("size")))
            except (TypeError, ArithmeticError):
                continue
            prints += 1
            fills.extend(sim.observe(tok, price, size, at))

    marked: list[dict] = []
    for f in fills:
        series = bids.get(f["token"]) or []
        for h in horizons:
            later = [p for (t, p) in series if t >= f["at"] + h]
            if not later:
                continue        # right-censored: no mark, and never a zero
            marked.append({**f, "horizon": h,
                           "markout": markout(f, later[0],
                                              _maker_fee(cfg, f["price"]))})
    return {"fills": len(fills), "marked": len(marked), "prints": prints,
            "tokens": len(tokens), "bracket": bracket(marked)}


def l12_maker(cfg, con, hist) -> LaneResult:
    """LaneResult wrapper so the maker bracket ranks beside L1-L11."""
    from pathlib import Path

    from arb.pm_tape import open_tape

    r = LaneResult(lane="L12", name="maker bracket (PM WS tape)",
                   primary="conservative-queue maker markout per contract",
                   unit="simulated fill")
    path = Path(str(cfg["maker"]["tape_db"]))
    if not path.exists():
        r.note = ("no tape recorded: run `arb pm-tape --record-secs 3600` "
                  "(keyless, 0 credits)")
        return r
    res = replay(cfg, open_tape(path))
    cons, opt = res["bracket"]["conservative"], res["bracket"]["optimistic"]
    r.n = int(cons["n"])
    r.estimate, r.lo, r.hi = cons["mean"], cons["lo"], cons["hi"]
    r.detail.append(f"{res['prints']} trade prints over {res['tokens']} tokens "
                    f"-> {res['fills']} simulated fills, {res['marked']} marked")
    r.detail.append(f"optimistic bound: n={opt['n']} mean={opt['mean']}")
    r.note = ("bracket, not a point estimate — queue position is unobservable, "
              "so the primary assumes we are LAST in queue and the optimistic "
              "bound assumes first; the truth is between")
    floor = int(cfg["maker"]["min_fills_for_verdict"])
    if r.n < floor:
        r.note += f"; under min_fills_for_verdict ({floor}) — record a longer tape"
        return r
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r
