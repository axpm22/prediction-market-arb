"""L14 row shaping for the whale lane, in both directions: the tape rows it
reads (markets, prints, signals, a booked bet's context), the ledger rows it
writes (`quotes`, candidates, the exit event), and the two shapers that turn a
fetched book into what the pure signal module takes (`_touch`) and gives back
(`_zombied`, §7.5 — the zombie test needs the ledger's volume, which a pure
module has no way to read). Split out of arb/lanes/whale.py on size alone
(modules stay under ~300 lines) — same rules: money is Decimal (rule 5), every
threshold comes from config (rule 4), no venue call and no clock (`now` is
always an argument).

The leg convention is spec §8, pinned: a WHALE leg always BUYS one token, so
`side = "YES"` and `outcome` is that token's own label — exactly how the watch
lane writes Polymarket sports quotes, which is what lets markouts join.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from decimal import Decimal

# The one best-of-book for a Polymarket CLOB book already lives in the
# political lane; a second copy is a second place for the ask to be wrong.
from arb.lanes.political import _best
from arb.logutil import get_logger
from arb.models import position_key, q6
from arb.venues.fees import leg_fee_cost
from arb.whale_signal import Signal, Touch
from arb import whale_sizing, whale_universe

log = get_logger("arb.whale", "logs/whale.log")
VENUE = "polymarket"
# tape `signals` DDL: arb/whale_tape.py SCHEMA (kept in sync by hand, as the
# collector's own row modules are).
SIGNAL_COLS = ("evaluated_at, condition_id, side, variant, points_yes, points_no,"
               " ratio, whale_ids_json, vwap, touch_ask, touch_bid, depth_usd,"
               " fired, suppressed_reason, flagged_edge, holders_ratio")


def _touch(books, tokens, cid) -> dict:
    """One `Touch` per side, keyed by outcome index. A side whose token or
    book is missing is simply absent, which `evaluate` reads as one-sided."""
    out = {}
    for side in (0, 1):
        best = _best(books.get(tokens.get((cid, side))))
        if best is not None:
            out[side] = Touch(ask=best[0], bid=best[1], depth_usd=best[2])
    return out


def _zombied(cfg, sig, volume) -> Signal:
    """§7.5, lane-level: the pure signal module knows nothing about zombies.
    Applied only to a signal that WOULD fire, so an earlier gate keeps its own
    (more informative) reason — the same order the spec lists them in.
    `volume_24h` is the ledger's latest watch quote for that token; unknown
    (no watch row) reads as NOT a zombie, never as one."""
    if not sig.fired or volume is None or volume != 0:
        return sig
    if sig.touch_ask is None or sig.touch_bid is None:
        return sig
    if sig.touch_ask - sig.touch_bid < Decimal(str(cfg["zombie_spread_min"])):
        return sig
    return dataclasses.replace(sig, fired=False, suppressed_reason="zombie")


def best_bid(book: dict | None) -> tuple[Decimal | None, Decimal]:
    """(best bid, depth in USD at that bid) — the exit price basis (§9).
    `_best` prices the ASK side; an exit sells into the bid."""
    bids = (book or {}).get("bids") or []
    if not bids:
        return None, Decimal(0)
    bid = max(Decimal(b["price"]) for b in bids)
    size = [b for b in bids if Decimal(b["price"]) == bid][0]["size"]
    return bid, q6(Decimal(size) * bid)


def _fetch_books(cfg, fetch, priced) -> tuple[dict, bool, float | None]:
    """A venue hiccup is a poll with no books, never a dead lane: the tracker
    reads the venue status and CARRIES open bets instead of closing them.
    The third element is the MEASURED /books latency in ms — None when no
    fetch happened at all, which is not the same fact as 0 ms.
    Moved from arb/lanes/whale.py on size alone (whale-mapper fix pass
    2026-09-04)."""
    tokens = [t for m in priced.values() for t, _ in m.tokens]
    if not tokens:
        return {}, True, None
    t0 = time.monotonic()
    try:
        return fetch(cfg, tokens), True, (time.monotonic() - t0) * 1000
    except Exception as e:  # noqa: BLE001 — venue outage, not a lane failure
        log.warning("whale books fetch failed for %d token(s): %s", len(tokens), e)
        return {}, False, (time.monotonic() - t0) * 1000


def quote_rows(cfg, market, books, event_key, now) -> list[dict]:
    """Both tokens of one market as `quotes` rows (rule 7): every book this
    lane fetches is persisted, flagged or not, and markouts attach because the
    key tuple is the one the bet's leg carries."""
    rows = []
    for token, label in market.tokens:
        best = _best(books.get(token))
        if best is None:
            continue
        ask, bid, depth = best
        rows.append({
            "fetched_at": now.isoformat(), "event_key": event_key,
            "category": market.root, "sport": market.sport, "venue": VENUE,
            "book": VENUE, "market_type": market.market_type, "line": market.line,
            "outcome": label, "side": "YES", "raw_price": ask, "cost": ask,
            "fee_cost": leg_fee_cost(cfg, VENUE, market.root, ask),
            "depth_usd": depth, "ladder_json": None, "live": False, "bid": bid,
            # unknown at fetch time: /books carries no 24h volume, and copying
            # the watch lane's number onto our timestamp would date a fact we
            # did not observe. The zombie guard reads the watch rows directly.
            "volume_24h": None,
            "start_utc": market.start_utc, "start_grade": "nominal",
            "venue_market_id": f"{market.condition_id}:{token}"})
    return rows


def fingerprint(event_key, market, label, variant, sizing) -> str:
    """§8: `sha1(base|variant|sizing)`, so the six variants and the three
    sizings (`whale_sizing.SIZINGS`) of one fired signal can never collide
    into one bet."""
    base = "|".join([event_key, "WHALE", VENUE, market.market_type or "",
                     market.line or "", label, "YES", variant, sizing])
    return hashlib.sha1(base.encode()).hexdigest()


def candidate(cfg, market, sig, sizing, signal_id, event_key, token, label,
              start, now) -> dict:
    """One `process_poll` candidate (the `candidate_from_opp` dict shape; no
    Opportunity is built). The price IS the touch the fire rule scored, so the
    booked cost can never drift from the signal that justified it."""
    ask, bid, depth = sig.touch_ask, sig.touch_bid, sig.depth_usd
    fee = leg_fee_cost(cfg, VENUE, market.root, ask)
    stake = whale_sizing.stake_for(sizing, sig.S, ask, cfg)
    cost_sum = q6(ask + fee)
    leg = {"venue": VENUE, "book": VENUE, "market_type": market.market_type,
           "line": market.line, "outcome": label, "side": "YES",
           "venue_side": "YES", "cost": str(ask), "fee_cost": str(fee),
           "bid": str(bid) if bid is not None else None,
           "depth_usd": str(depth),
           "mid0": str(q6((ask + bid) / 2)) if bid is not None else None,
           "venue_market_id": f"{market.condition_id}:{token}",
           "fetched_at": now.isoformat()}
    return {
        "kind": "WHALE", "category": market.root, "sport": market.sport,
        "event_key": event_key, "event_start_utc": start,
        "start_grade": "nominal", "gamepk": None, "venue_pair": f"{VENUE}:self",
        "fingerprint": fingerprint(event_key, market, label, sig.variant, sizing),
        # §8 (pinned 2026-09-04): the generic key with a `|<variant>/<sizing>`
        # suffix. Each variant x sizing is its own paper position by design, and
        # the tracker dedups active bets by position_key — without the suffix a
        # variant whose first fire lands on a later cycle than another's is
        # absorbed as a re-sighting of it and never booked.
        "position_key": position_key("WHALE", market.sport, event_key,
                                     market.market_type, market.line,
                                     outcome=label, side="YES")
                        + f"|{sig.variant}/{sizing}",
        "legs": [leg], "edge_points": sig.flagged_edge, "cost_sum": cost_sum,
        "payout": Decimal(1), "roi": q6(Decimal(1) / cost_sum - 1),
        "status": "OK", "quality_flag": "none", "leg_skew_secs": 0.0,
        "depth_ok": True,        # the lane gated depth before it ever fired
        "proof_status": None, "live": False, "pregame": True,
        "stake_usd": stake, "variant": f"{sig.variant}/{sizing}",
        "signal_id": signal_id,
        "notes": f"whale {sig.variant}/{sizing}; {len(sig.whales)} whale(s); "
                 f"ratio={sig.ratio}; S={sig.S}"}


def note_no_bid(store, bet, decision, bid, depth, venue_ok, now) -> None:
    """§9: no bid, or depth under `min_executable_usd` — no exit, an
    annotation, re-evaluated next cycle. Written ONCE per (bet, state): an
    outage lasts hours, and the re-evaluation is the cycle's own counter, not
    a new row every 60 s. `venue_ok` tells a THIN BOOK (the venue answered,
    nobody is bidding) from an OUTAGE (no book at all): the same annotation,
    opposite causes."""
    detail = (f"exit:{decision.reason} priced at bid={bid} depth={depth} < "
              f"min_executable_usd; venue_ok={venue_ok}; re-evaluated next cycle")
    last = store.query("SELECT kind, detail FROM annotations WHERE target_kind='bet'"
                       " AND target_id=? ORDER BY id DESC LIMIT 1", (str(bet["id"]),))
    if last and (last[0]["kind"], last[0]["detail"]) == ("exit-no-bid", detail):
        return
    store.insert("annotations", {
        "created_at": now.isoformat(), "target_kind": "bet",
        "target_id": str(bet["id"]), "kind": "exit-no-bid", "detail": detail})


def exit_payload(cfg, bet, decision, bid, contracts) -> dict:
    """§9's terminal `settled` payload. `result='EXITED'` and a `source` with
    the `exit:` prefix, which metrics.bet_pnl_optimistic reads for its pnl and
    the instant-settle tripwire exempts."""
    fee = leg_fee_cost(cfg, VENUE, bet["category"], bid)
    stake = Decimal(str(bet["stake_usd"]))
    return {"result": "EXITED", "source": f"exit:{decision.reason}",
            "exit_price": str(bid), "exit_fee": str(fee),
            "pnl": str(q6((bid - fee) * contracts - stake)),
            # a `confidence` exit names the whale, the level and the n that
            # moved the score: pooling means another sport can cause it (§9)
            "detail": {k: str(v) if isinstance(v, Decimal) else v
                       for k, v in decision.detail.items()}}


# ------------------------------------------------------------- tape rows

# V4's filter (§7, amended 2026-09-04). Keyed on the DATE, not the exact
# timestamp (fix 2026-09-04, V4 review of 3a455fd): each of the day's four
# windows is claimed and stamped with its OWN `now`
# (whale_backfill._snapshot_window), so a retry after a failed window lands
# the remaining windows LATER the same UTC day. An exact `snapshot_at = MAX`
# match then returned only the retried windows' wallets and silently dropped
# whoever was ranked only in the windows claimed earlier that day. The ranked
# set is every wallet from every row whose calendar date (UTC) equals the
# latest snapshot date present — any window, still never yesterday's board.
RANKED_SQL = """
    SELECT DISTINCT proxy_wallet FROM leaderboard_snapshots
     WHERE date(snapshot_at) = (SELECT date(MAX(snapshot_at)) FROM leaderboard_snapshots)
       AND proxy_wallet <> ''"""


def ranked_wallets(con) -> frozenset:
    """Loaded once per cycle and handed to `evaluate`/`exit_reason`: a
    membership test on top of the qualification bar, never a score input."""
    return frozenset(r[0] for r in con.execute(RANKED_SQL))


def persist_base_rates(con, state, now) -> None:
    """The one WRITE in the scoring chain (§6): each root's base rate with the
    `as_of` that makes a score reproducible — which is why the report calls
    `score_state` directly and never this. Moved off arb/lanes/whale.py to keep
    that module under ~300 lines."""
    for root, value in state.rates.items():
        con.execute("INSERT OR REPLACE INTO base_rates (root, as_of, value, n) "
                    "VALUES (?,?,?,?)",
                    (root, now.isoformat(), str(value), state.rate_n[root]))
    con.commit()


def _tape_market(row) -> whale_universe.Market:
    """A market we hold a bet in but that has aged out of the universe still
    needs its tokens, so it can still be priced and exited."""
    return whale_universe.Market(
        condition_id=row["condition_id"], sport=row["sport"],
        market_type=row["market_type"], line=row["line"],
        start_utc=row["start_utc"], event_key=row["event_key"],
        tokens=tuple((t[0], t[1]) for t in json.loads(row["tokens_json"] or "[]")))


def _from_prints(prints) -> tuple[dict, dict]:
    """(token by (condition_id, outcome_index), event_slug by condition_id).
    Both are venue facts carried on every print row — the ledger's quotes give
    us the tokens but never their outcome INDEX, which is what a side is."""
    tokens, slugs = {}, {}
    for p in prints:
        key = (p["condition_id"], p["outcome_index"])
        if p["asset"] and key not in tokens:
            tokens[key] = p["asset"]
        if p["event_slug"] and p["condition_id"] not in slugs:
            slugs[p["condition_id"]] = p["event_slug"]
    return tokens, slugs


def _event_key(cid, slugs, tape_markets) -> str:
    """§8: `pm:<event_slug>` always — one game's moneyline and total share it,
    so it doubles as the cluster key for every CI. Falls back to the market's
    own slug, then to the row's own `event_key` when that is already in
    `pm:` form, then to the condition id.

    The `pm:` step is the non-sports one (M1, fix pass): a tag-builder row has
    no `slug` (Gamma's by-tag answer never fills it) but does carry the
    finished `pm:<event_slug>`, so without it a market whose prints have aged
    out — every open non-sports bet waiting on a markout — keyed its quotes on
    the condition id and stopped clustering with its own bet. A SPORTS row's
    stored `event_key` is the ledger's CROSS-VENUE key, which §8 says never to
    use, and the prefix test is exactly what excludes it."""
    row = tape_markets.get(cid) or {}
    slug = slugs.get(cid) or row.get("slug")
    if slug:
        return f"pm:{slug}"
    stored = str(row.get("event_key") or "")
    return stored if stored.startswith("pm:") else f"pm:{cid}"


def _persist_signal(con, sig, cid, now) -> int:
    """Every evaluation, fired or not: suppressed signals are this lane's
    'left on the table' (§7). `bet_ids_json` stays NULL — the bets carry
    `signal_id`, and that join is the source of truth in both directions."""
    cur = con.execute(
        f"INSERT INTO signals ({SIGNAL_COLS}) VALUES ({','.join('?' * 16)})",
        (now.isoformat(), cid, str(sig.side), sig.variant, str(sig.points_yes),
         str(sig.points_no), str(sig.ratio), json.dumps(sig.whales, default=str),
         str(sig.vwap), _s(sig.touch_ask), _s(sig.touch_bid), str(sig.depth_usd),
         int(sig.fired), sig.suppressed_reason, str(sig.flagged_edge),
         _s(sig.holders_ratio)))
    con.commit()
    return cur.lastrowid


def _s(v):
    return None if v is None else str(v)


def _bet_ctx(con, bet) -> dict | None:
    """What §9 re-evaluates a booked bet against: its variant, its side, its
    market and the whales that formed it — rebuilt from the bet's own leg and
    its `signals` row, never from anything recomputed since."""
    if bet["signal_id"] is None:
        return None
    row = con.execute("SELECT * FROM signals WHERE id=?", (bet["signal_id"],)).fetchone()
    if row is None:
        return None
    leg = json.loads(bet["legs_json"])[0]
    cid, _, token = (leg.get("venue_market_id") or "").partition(":")
    return {"variant": row["variant"], "side": int(row["side"]), "condition_id": cid,
            "token": token, "start_utc": bet["event_start_utc"],
            "whales": json.loads(row["whale_ids_json"] or "[]")}
