"""L14 whale-consensus lane: one `whale --once` cycle, spec §5 steps 1-9.

Two write paths, never mixed: the collector and scorer own the tape
(`whale.tape_db`); this module books through `arb.tracker.process_poll` into
the ledger like every other lane. Nothing here writes an existing row —
the exit is an appended `bet_events` row, never `settle_bet`, whose
future-start guard rejects the pregame settlement an exit always is (§9).

Rule 1: public GETs only (`fetch_books`, the print and resolution readers).
Rule 4: every threshold from config; the depth floor is `min_executable_usd`.
Rule 7: every book fetched, at signal OR exit time, becomes `quotes` rows.
Rule 9: ledger writes go through Store.insert / process_poll, append-only.
Rule 12: `classify()` already returns review for WHALE; the guards here decide
        whether to book at all, never which lane.

Backfill and the leaderboard snapshot are NOT in this cycle: they are the
separate `whale-backfill --once` process (§5 step 4 — Task 3 measured 353
calls / ~11 min per backfill cycle against a 60 s cadence here).
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from decimal import Decimal

from arb import whale_backfill, whale_exit, whale_resolve, whale_score, whale_signal
from arb import whale_tape
from arb.lanes.political import _best
from arb.lanes.whale_book import (_bet_ctx, _event_key, _from_prints,
                                  _persist_signal, _tape_market, best_bid,
                                  candidate, exit_payload, quote_rows)
from arb.logutil import get_logger
from arb.replay import open_ro
from arb.venues.base import get_json
from arb.venues.polymarket import fetch_books
from arb.whale_hygiene import hygiene_flags
from arb.whale_merge import qualifying
from arb.whale_signal import Touch

log = get_logger("arb.whale", "logs/whale.log")

# The ledger's PM volume for ONE poll: served by idx_quotes_poll, exactly like
# whale_tape.UNIVERSE_SQL. A scan by venue_market_id would read 37 GB.
VOLUME_SQL = (
    "SELECT venue_market_id, volume_24h FROM quotes "
    "WHERE poll_id = (SELECT MAX(id) FROM polls WHERE kind='watch') "
    "AND venue='polymarket' AND category='sports'")
OPEN_BETS_SQL = """
    SELECT * FROM flat_bets fb WHERE fb.kind='WHALE' AND fb.poll_kind='whale'
      AND NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id = fb.id
                      AND be.kind IN ('settled', 'void', 'lifetime_closed'))"""


def _d(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


# ------------------------------------------------------------------ cycle IO

def _scorer(cfg, con, positions, resolutions, now):
    """(score_lookup, base rate per root). The base rate is persisted with its
    `as_of` so a score is reproducible (§6)."""
    outcomes = whale_score.resolved_outcomes(positions, resolutions, now)
    agg = whale_score.aggregate(outcomes)
    rates = {}
    for root in {o.root for o in outcomes} | {"sports"}:
        value, n = whale_score.base_rate(outcomes, root, now, cfg)
        rates[root] = value
        con.execute("INSERT OR REPLACE INTO base_rates (root, as_of, value, n) "
                    "VALUES (?,?,?,?)", (root, now.isoformat(), str(value), n))
    con.commit()

    def lookup(wallet, sport, market_type):
        return whale_score.score_for(agg, wallet, sport, market_type, cfg, rates)
    return lookup, rates


def _calibration_blocked(con, cfg) -> set[str]:
    """§7.2: `min_points.V2/V3` may be null only while the tape's `signals`
    span fewer than `fire.calibration_days` distinct UTC days — a day with no
    rows is an outage day and simply never counts. Past that with a null we
    log CRITICAL once per cycle and book nothing for that variant (`evaluate`
    already suppresses it as `uncalibrated`; this is the belt)."""
    fire = cfg["whale"]["fire"]
    nulls = {v for v, x in fire["min_points"].items() if x is None}
    if not nulls:
        return set()
    days = con.execute(
        "SELECT COUNT(DISTINCT substr(evaluated_at, 1, 10)) d FROM signals"
    ).fetchone()["d"]
    if days < int(fire["calibration_days"]):
        return set()
    for variant in sorted(nulls):
        log.critical("whale.fire.min_points.%s is still null after %d days of "
                     "signals (calibration_days=%s) — booking nothing for %s "
                     "until the dated amendment lands (design §7.2)",
                     variant, days, fire["calibration_days"], variant)
    return nulls


def _touch(books, tokens, cid) -> dict:
    """One `Touch` per side, keyed by outcome index. A side whose token or
    book is missing is simply absent, which `evaluate` reads as one-sided."""
    out = {}
    for side in (0, 1):
        best = _best(books.get(tokens.get((cid, side))))
        if best is not None:
            out[side] = Touch(ask=best[0], bid=best[1], depth_usd=best[2])
    return out


def _zombied(cfg, sig, volume) -> "whale_signal.Signal":
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


# ---------------------------------------------------------------- the cycle

def run_whale_cycle(cfg, store, con, now, get=get_json, fetch=fetch_books) -> dict:
    """One 60 s cycle. `con` is the tape handle (`whale_tape.open_tape`)."""
    w = cfg["whale"]
    res = {"status": "disabled", "markets": 0, "prints": 0, "bad_rows": 0,
           "wallets": 0, "hygiene_excluded": 0, "signals_evaluated": 0, "fired": 0,
           "suppressed": {}, "booked": 0, "exited": 0, "exit_no_bid": 0,
           "quotes_rows": 0}
    if not w["enabled"]:
        return res
    ro = open_ro(store.path)

    # 1-3: universe, prints, discovery.
    markets = whale_tape.universe(ro, cfg, now)
    poll = whale_tape.poll_prints(cfg, con, markets, now, get=get)
    res.update(status=poll["status"], markets=len(markets), prints=poll["rows"],
               bad_rows=poll["bad_rows"],
               wallets=whale_backfill.discover_wallets(cfg, con, now))

    # 5: resolutions for tracked markets and for the markets we hold bets in.
    bets = [dict(b) for b in store.query(OPEN_BETS_SQL)]
    ctxs = {b["id"]: _bet_ctx(con, b) for b in bets}
    whale_resolve.read_resolutions(
        cfg, con, [m.condition_id for m in markets]
        + [c["condition_id"] for c in ctxs.values() if c], now, get=get)

    # 6: score. Hygiene is applied by the LANE, before scoring and signalling
    # (Task 5 decision), so an excluded wallet never becomes a vote.
    # ponytail: positions are rebuilt from the whole tape every cycle — fine
    # while the tape is our own collection; cache the aggregation per wallet if
    # backfill ever makes this outrun the 120 s runtime cap.
    prints = [dict(r) for r in con.execute("SELECT * FROM prints")]
    tape_markets = {r["condition_id"]: dict(r)
                    for r in con.execute("SELECT * FROM markets")}
    resolutions = {r["condition_id"]: dict(r)
                   for r in con.execute("SELECT * FROM resolutions")}
    positions = whale_score.positions_from_prints(prints, tape_markets, cfg)
    hyg = hygiene_flags(prints, positions, cfg)
    open_pos = [p for p in positions if not hyg.excludes(p)]
    res["hygiene_excluded"] = len(positions) - len(open_pos)
    lookup, rates = _scorer(cfg, con, open_pos, resolutions, now)
    tokens, slugs = _from_prints(prints)

    # 8-9 share one fetch: the same books price a new signal and an exit, and
    # §8 wants a quotes row every cycle for a market with an open WHALE bet so
    # markouts attach. `qualifying` here is only a filter — `evaluate` derives
    # its own whales — but it keeps books calls off markets that cannot fire.
    live = {m.condition_id: m for m in markets
            if qualifying(m.condition_id, open_pos, lookup, now, cfg)}
    priced = dict(live)
    for ctx in ctxs.values():
        cid = ctx["condition_id"] if ctx else None
        if cid and cid not in priced and cid in tape_markets:
            priced[cid] = _tape_market(tape_markets[cid])
    books, venue_ok = _fetch_books(cfg, fetch, priced)
    keys = {cid: _event_key(cid, slugs, tape_markets) for cid in priced}
    rows = [r for cid, m in priced.items()
            for r in quote_rows(cfg, m, books, keys[cid], now)]
    res["quotes_rows"] = len(rows)

    # 7-8: evaluate, persist every variant, book the fired ones twice.
    volumes = {r["venue_market_id"]: _d(r["volume_24h"]) for r in ro.execute(VOLUME_SQL)}
    blocked = _calibration_blocked(con, cfg)
    cands = []
    for cid, m in live.items():
        market = {"condition_id": cid, "start_utc": m.start_utc,
                  "sport": m.sport, "market_type": m.market_type}
        m0 = rates.get(whale_score.root_of(m.sport, m.market_type),
                       w["score"]["base_rate_default"])
        touch = _touch(books, tokens, cid)
        for sig in whale_signal.evaluate(market, open_pos, lookup, touch, now, cfg, m0):
            token = tokens.get((cid, sig.side))
            sig = _zombied(cfg, sig, volumes.get(f"{cid}:{token}"))
            signal_id = _persist_signal(con, sig, cid, now)
            res["signals_evaluated"] += 1
            if not sig.fired:
                reason = sig.suppressed_reason
                res["suppressed"][reason] = res["suppressed"].get(reason, 0) + 1
                continue
            res["fired"] += 1
            if sig.variant in blocked:
                continue
            label = dict(m.tokens).get(token) or ""
            start = datetime.fromisoformat(m.start_utc) if m.start_utc else None
            cands += [candidate(cfg, m, sig, sizing, signal_id, keys[cid], token,
                                label, start, now) for sizing in ("flat", "conf")]

    poll_id = _persist(cfg, store, rows, cands, venue_ok, now)
    res["booked"] = store.query(
        "SELECT COUNT(*) c FROM flat_bets WHERE poll_id=?", (poll_id,))[0]["c"]

    # 9: exits, on the books this cycle already fetched.
    for bet in bets:
        _exit(cfg, store, bet, ctxs[bet["id"]], open_pos, lookup, books, now, res)

    from arb.cli import head_sha
    from arb.heartbeat import beat
    from arb.markout import persist_marks
    persist_marks(cfg, store)
    beat(store, "whale", head_sha())
    log.info("whale cycle: %s", res)
    return res


def _fetch_books(cfg, fetch, priced) -> tuple[dict, bool]:
    """A venue hiccup is a poll with no books, never a dead lane: the tracker
    reads the venue status and CARRIES open bets instead of closing them."""
    tokens = [t for m in priced.values() for t, _ in m.tokens]
    if not tokens:
        return {}, True
    try:
        return fetch(cfg, tokens), True
    except Exception as e:  # noqa: BLE001 — venue outage, not a lane failure
        log.warning("whale books fetch failed for %d token(s): %s", len(tokens), e)
        return {}, False


def _persist(cfg, store, rows, cands, venue_ok, now) -> int:
    """arb.cycle.persist_poll's sequence, on dict quotes and dict candidates
    (that function needs Quote/Opportunity objects; the political lane's
    command mirrors it the same way)."""
    from arb.tracker import process_poll
    poll_id = store.insert("polls", {"started_at": now.isoformat(), "kind": "whale",
                                     "n_quotes": len(rows), "n_opps": len(cands)})
    store.insert_many("poll_venues", [{"poll_id": poll_id, "venue": "polymarket",
                                       "ok": venue_ok, "n_quotes": len(rows),
                                       "latency_ms": 0}])
    store.insert_many("quotes", [{**r, "poll_id": poll_id} for r in rows])
    process_poll(cfg, store, poll_id, "whale", cands,
                 {"polymarket": {"ok": venue_ok, "n": len(rows), "latency_ms": 0}},
                 now=now)
    return poll_id


def _exit(cfg, store, bet, ctx, open_pos, lookup, books, now, res) -> None:
    """§9 for one open bet. `exit_reason` returns None post-start (a bet is
    held to resolution then), so there is nothing extra to re-check here."""
    if ctx is None:
        return
    decision = whale_exit.exit_reason(ctx, open_pos, lookup, now, cfg)
    if decision is None:
        return
    bid, depth = best_bid(books.get(ctx["token"]))
    if bid is None or depth < Decimal(str(cfg["min_executable_usd"])):
        store.insert("annotations", {
            "created_at": now.isoformat(), "target_kind": "bet",
            "target_id": str(bet["id"]), "kind": "exit-no-bid",
            "detail": f"exit:{decision.reason} priced at bid={bid} "
                      f"depth={depth} < min_executable_usd; re-evaluated next cycle"})
        res["exit_no_bid"] += 1
        return
    # contracts on the same basis metrics.bet_pnl_optimistic counts them for a
    # resolution-settled kind: stake / (cost + fee) per leg.
    from arb.metrics import bet_effective_cost
    contracts = Decimal(str(bet["stake_usd"])) / bet_effective_cost(bet)
    store.insert("bet_events", {
        "bet_id": bet["id"], "created_at": now.isoformat(), "kind": "settled",
        "payload_json": json.dumps(exit_payload(cfg, bet, decision, bid,
                                                contracts))})
    res["exited"] += 1
    log.info("whale exit %s on bet %s at bid %s", decision.reason, bet["id"], bid)
