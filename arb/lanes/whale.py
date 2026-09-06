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

import json
import time
from datetime import datetime
from decimal import Decimal

from arb import whale_backfill, whale_dominance, whale_elite, whale_exit, whale_holdings
from arb import (whale_mat, whale_ns_lane, whale_resolve, whale_score,
                 whale_signal, whale_sizing, whale_tape, whale_universe)
from arb.lanes.whale_book import (_bet_ctx, _event_key, _fetch_books,
                                  _persist_signal, _tape_market, _touch,
                                  _zombied, best_bid, candidate, exit_payload,
                                  market_tokens, note_no_bid,
                                  persist_base_rates, quote_rows, ranked_wallets)
from arb.logutil import get_logger
from arb.replay import open_ro
from arb.venues.base import get_json
from arb.venues.polymarket import fetch_books
from arb.whale_merge import qualifying
from arb.whale_state import score_state
from arb.whale_tape import _set_meta

log = get_logger("arb.whale", "logs/whale.log")

# The ledger's PM volume for ONE poll: served by idx_quotes_poll, exactly like
# whale_universe.UNIVERSE_SQL. `poll_id` is bound to the SAME fallback poll
# `universe()` picked (`_poll_with_pm_rows`) — never MAX(id), or an empty
# newest poll starves volumes and the zombie guard reads volume=None (unknown
# -> not-a-zombie) and fails open lane-wide (2026-09-04).
VOLUME_SQL = (
    "SELECT venue_market_id, volume_24h FROM quotes "
    "WHERE poll_id = ? AND venue='polymarket' AND category='sports'")
# §9 (pinned 2026-09-04): open FOR EXIT PURPOSES means no terminal event.
# `lifetime_closed` is deliberately NOT terminal here — the tracker writes it
# after `dead_checks` cycles with no re-sighting, which for this lane only
# means the prints aged past max_print_age_secs, not that the position closed.
OPEN_BETS_SQL = """
    SELECT * FROM flat_bets fb WHERE fb.kind='WHALE' AND fb.poll_kind='whale'
      AND NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id = fb.id
                      AND be.kind IN ('settled', 'void'))"""


def _d(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


# ------------------------------------------------------------------ cycle IO

def _calibration_blocked(con, cfg) -> set[str]:
    """§7.2: `min_points.V2/V3` may be null only while the tape's `signals`
    span `fire.calibration_days` distinct UTC days or fewer — a day with no
    rows is an outage day and simply never counts, and 'null for the first 14
    days' reads literally: the guard fires on day 15. Past that with a null we
    log CRITICAL once per cycle and book nothing for that variant (`evaluate`
    already suppresses it as `uncalibrated`; this is the belt)."""
    fire = cfg["whale"]["fire"]
    nulls = {v for v, x in fire["min_points"].items() if x is None}
    if not nulls:
        return set()
    days = con.execute(
        "SELECT COUNT(DISTINCT substr(evaluated_at, 1, 10)) d FROM signals"
    ).fetchone()["d"]
    if days <= int(fire["calibration_days"]):
        return set()
    for variant in sorted(nulls):
        log.critical("whale.fire.min_points.%s is still null after %d days of "
                     "signals (calibration_days=%s) — booking nothing for %s "
                     "until the dated amendment lands (design §7.2)",
                     variant, days, fire["calibration_days"], variant)
    return nulls


# ---------------------------------------------------------------- the cycle

def run_whale_cycle(cfg, store, con, now, get=get_json, fetch=fetch_books) -> dict:
    """One 60 s cycle. `con` is the tape handle (`whale_tape.open_tape`)."""
    w = cfg["whale"]
    res = {"status": "disabled", "markets": 0, "prints": 0, "bad_rows": 0,
           "wallets": 0, "hygiene_excluded": 0, "signals_evaluated": 0, "fired": 0,
           "suppressed": {}, "booked": 0, "exited": 0, "exit_no_bid": 0,
           # the two `left` suppressions of the 2026-09-06 fix, on the cycle
           # line so the audit can watch what the old rules would have exited:
           # (bet, wallet) pairs the venue has no reading for, and bets whose
           # `left` was not evaluated because the positions cache is partial.
           "exit_left_no_venue_reading": 0, "exit_skipped_cache_partial": 0,
           "quotes_rows": 0, "market_errors": 0, "label_collision": 0, "poll_id": None,
           "mat": {},
           "holdings": {}}
    if not w["enabled"]:
        return res
    t0 = time.monotonic()
    ro = open_ro(store.path)

    # 1-3: universe, prints, discovery. `universe()` already falls back across
    # a bounded lookback of watch polls; empty here means none had a PM row.
    # poll_prints would still burn a `/trades` call (global mode pages
    # unconditionally) and advance the watermark past an unfiltered window, a
    # gap the 120s overlap can't fully recover — skip it, watermark untouched.
    # discover_wallets reads the tape only (no network), so it runs regardless.
    markets = (whale_universe.universe(ro, cfg, now, res=res)
               + whale_ns_lane.tape_universe(con, cfg, now))
    if markets:
        poll = whale_tape.poll_prints(cfg, con, markets, now, get=get)
        res.update(status=poll["status"], markets=len(markets), prints=poll["rows"],
                   bad_rows=poll["bad_rows"])
    else:
        log.warning("whale cycle: empty market universe (no watch poll in the "
                    "lookback window had Polymarket sports rows, and no "
                    "non-sports market inside whale.nonsports.max_days) — skipping "
                    "poll_prints, watermark untouched")
        res["status"] = "no-universe"
    res["wallets"] = whale_backfill.discover_wallets(cfg, con, now)

    # 5: resolutions for tracked markets and for the markets we hold bets in.
    bets = [dict(b) for b in store.query(OPEN_BETS_SQL)]
    ctxs = {b["id"]: _bet_ctx(con, b) for b in bets}
    whale_resolve.read_resolutions(
        cfg, con, [m.condition_id for m in markets]
        + [c["condition_id"] for c in ctxs.values() if c], now, get=get)

    # 5b (L14 amendment 2026-09-04, §9 item C): what the signal whales behind
    # an open bet still HOLD, straight from data-api. Bounded to
    # exit.holdings_calls_per_cycle wallets a cycle and paced by the same
    # bucket as every other call here; it must run BEFORE the exits below, so
    # this cycle's exits read this cycle's snapshot.
    # `tracked`: retention fix 2026-09-05 — this cycle's live universe union
    # every open bet's own market, so `for_bet` below still finds a bet's
    # holding even for a market that has since dropped out of the live
    # universe. Passed explicitly (`holdings_keep: universe`) rather than
    # left to derive from the tape's `markets` table, which is every market
    # backfill has EVER mapped (14k+ rows) and made a signal whale's whole
    # HISTORICAL portfolio "tracked" (measured: +57,844 holdings rows/hour).
    tracked = {m.condition_id for m in markets} | {c["condition_id"] for c in ctxs.values() if c}
    res["holdings"] = whale_holdings.poll_holdings(
        cfg, con, whale_holdings.wallets_of(ctxs.values()), now, tracked, get=get)

    # 6: score. Hygiene is applied by the LANE, before scoring and signalling
    # (Task 5 decision), so an excluded wallet never becomes a vote. The
    # assembly itself is shared with the report (arb/whale_state.py).
    # matpos (2026-09-05): the cycle brings the derived positions/hygiene cache
    # up to date FIRST — bounded by whale.mat.max_wallets_per_cycle, so a first
    # pass over an existing tape spreads over cycles instead of stalling this
    # one — and `score_state` then reads that cache instead of every print.
    # This is the only writer of it in the lane's own process (arb/whale_mat.py).
    res["mat"] = whale_mat.materialize(con, cfg, now)
    state = score_state(cfg, con, now)
    tape_markets = state.markets
    open_pos = state.open_positions
    res["hygiene_excluded"] = state.hygiene_excluded
    persist_base_rates(con, state, now)
    lookup, rates = state.lookup, state.rates
    # V4 (§7, amended 2026-09-04): one read per cycle, shared by the signals
    # this cycle evaluates and the exits it re-evaluates, so both sides of a
    # V4 position are judged against the same board. V5's elite cutoff is the
    # same idea over the scoring pass above — one bar per bucket per cycle,
    # computed on first use and persisted below.
    ranked = ranked_wallets(con)
    elite = whale_elite.index(cfg, state)
    # ... and whether that cutoff's population is the tape's yet. Read ONCE
    # per cycle, off the queue this cycle's `materialize` just drained (fix
    # pass 2026-09-05, I1: a partial population can admit a whale the full one
    # denies, so V5 does not fire while a full rebuild is still draining).
    partial = whale_mat.cache_partial(con)

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
    # The token/event_slug maps, read for the markets this cycle prices and no
    # others (matpos 2026-09-05 — this used to walk every print on the tape).
    # The union with the open bets' own markets is what makes that exact: an
    # aged-out held market that `priced` skipped (no `markets` row) is still a
    # cid `_exit` looks a token up for, through market_volume — so every key
    # any reader below asks for is in the scope of this read, and the maps are
    # the same maps for those keys.
    tokens, slugs = market_tokens(
        con, set(priced) | {c["condition_id"] for c in ctxs.values() if c})
    books, venue_ok, latency_ms = _fetch_books(cfg, fetch, priced)
    keys = {cid: _event_key(cid, slugs, tape_markets) for cid in priced}
    rows = [r for cid, m in priced.items()
            for r in quote_rows(cfg, m, books, keys[cid], now)]
    res["quotes_rows"] = len(rows)

    # 7-8: evaluate, persist every variant, book the fired ones twice.
    # I2 (mapper fix pass 2, 2026-09-04): bind to the SAME poll universe()
    # already picked (res["poll_id"]), never a fresh _poll_with_pm_rows call
    # — poll_prints/read_resolutions/_fetch_books above spend real network
    # time, in which the separate watch-lane process can commit a NEW poll;
    # a re-query would silently switch VOLUME_SQL to it mid-cycle.
    vol_poll = res["poll_id"]
    volumes = ({r["venue_market_id"]: _d(r["volume_24h"])
               for r in ro.execute(VOLUME_SQL, (vol_poll,))} if vol_poll is not None else {})
    whale_ns_lane.seed_volumes(volumes, markets)   # non-sports: off the tape
    blocked = _calibration_blocked(con, cfg)
    cands = []
    for cid, m in live.items():
        # One market's evaluate + signals row + candidates, isolated: a market
        # the venue or the tape has left malformed must not cost this cycle its
        # quotes rows, its exits, its polls row or its heartbeat.
        try:
            market = {"condition_id": cid, "start_utc": m.start_utc,
                      "sport": m.sport, "market_type": m.market_type}
            m0 = rates.get(whale_score.root_of(m.sport, m.market_type),
                           w["score"]["base_rate_default"])
            touch = _touch(books, tokens, cid)
            # V6's denominator: the market's own 24h volume, off the same
            # watch rows the zombie guard below reads (§7, 2026-09-04).
            for sig in whale_signal.evaluate(
                    market, open_pos, lookup, touch, now, cfg, m0, ranked,
                    whale_dominance.market_volume(volumes, tokens, cid),
                    elite(m.sport, m.market_type), cache_partial=partial):
                token = tokens.get((cid, sig.side))
                sig = _zombied(cfg, sig, volumes.get(f"{cid}:{token}"))
                signal_id = _persist_signal(con, sig, cid, now)
                res["signals_evaluated"] += 1
                if not sig.fired:
                    reason = sig.suppressed_reason
                    res["suppressed"][reason] = res["suppressed"].get(reason, 0) + 1
                    continue
                if sig.variant in blocked:      # counted as fired only if bookable
                    continue
                res["fired"] += 1
                label = dict(m.tokens).get(token) or ""
                start = datetime.fromisoformat(m.start_utc) if m.start_utc else None
                cands += [candidate(cfg, m, sig, sizing, signal_id, keys[cid], token,
                                    label, start, now)
                          for sizing in whale_sizing.SIZINGS]
        except Exception as e:  # noqa: BLE001 — one market, not the whole cycle
            res["market_errors"] += 1
            log.warning("whale market %s skipped: %s", cid, e, exc_info=True)

    # §9 (final review 2026-09-04): a position we still hold is never booked
    # again, whatever the tracker's lifetime bookkeeping says. Its own position
    # dedup only sees ACTIVE bets, and `lifetime_closed` drops a WHALE bet from
    # that set after `dead_checks` quiet cycles — while we are still in it — so
    # the next print re-books the same variant/sizing on top of the open one.
    # A candidate the tracker DOES still see is left in `cands` on purpose: it
    # absorbs that one as a re-sighting, which is where the confirmation price
    # and the lifetime bookkeeping come from (§8).
    from arb.tracker import _active_bets
    held = ({b["position_key"] for b in bets}
            - {b["position_key"] for b in _active_bets(store, "whale")})
    cands, on_table = ([c for c in cands if c["position_key"] not in held],
                       [c for c in cands if c["position_key"] in held])
    poll_id = _persist(cfg, store, rows, cands, on_table, venue_ok, latency_ms, now)
    res["booked"] = store.query(
        "SELECT COUNT(*) c FROM flat_bets WHERE poll_id=?", (poll_id,))[0]["c"]

    # 9: exits, on the books this cycle already fetched. A V5 bet is re-read
    # against THIS cycle's elite bar, from the same index the signals used.
    # `partial` gates the `left` rules (false-`left` fix 2026-09-06, F3): the
    # cycle that rebuilt this cache read the half-filled population as "every
    # whale left" and exited 28 bets at once. `state.positions` — UNFILTERED —
    # is what those rules read (F4): hygiene decides who votes, not who holds.
    if partial and bets:
        res["exit_skipped_cache_partial"] = len(bets)
        log.warning("whale exits: `left` not evaluated for %d open bet(s) — the "
                    "positions cache is still rebuilding, so an absent position "
                    "is no reading", len(bets))
    for bet in bets:
        ctx = ctxs[bet["id"]]
        mk = tape_markets.get(ctx["condition_id"], {}) if ctx else {}
        _exit(cfg, store, con, bet, ctx, open_pos, lookup, books, venue_ok, now,
              res, ranked, tokens, volumes,
              elite(mk.get("sport"), mk.get("market_type")),
              all_pos=state.positions, cache_partial=partial)
    whale_elite.persist(con, elite, cfg, now)

    # collector health for the daily report (§12): `polls` has no column for a
    # cycle's status/rows/bad_rows and rule 9 forbids inventing one, so the
    # counters live on the tape the report already opens read-only. Written
    # right after the cycle's own work — before persist_marks/beat — so a
    # markout failure below cannot leave this meta value stale.
    # duration_secs against supervisor.max_child_runtime_secs.whale (120): a
    # cycle drifting towards its own kill is visible before it is fatal.
    res["duration_secs"] = round(time.monotonic() - t0, 3)
    _set_meta(con, "last_cycle", json.dumps({**res, "at": now.isoformat()}))
    log.info("whale cycle: %s", res)

    from arb.cli import head_sha
    from arb.heartbeat import beat
    from arb.markout import persist_marks
    persist_marks(cfg, store)
    beat(store, "whale", head_sha())
    return res


def _persist(cfg, store, rows, cands, on_table, venue_ok, latency_ms, now) -> int:
    """arb.cycle.persist_poll's sequence, on dict quotes and dict candidates
    (that function needs Quote/Opportunity objects; the political lane's
    command mirrors it the same way). `on_table` are the candidates this cycle
    refused to book; they are recorded through the tracker's own `_lot`, so
    they land in `left_on_table` with every other refusal."""
    from arb.tracker import _lot, process_poll
    poll_id = store.insert("polls", {"started_at": now.isoformat(), "kind": "whale",
                                     "n_quotes": len(rows), "n_opps": len(cands)})
    for cand in on_table:
        _lot(store, poll_id, cand, "position-held")
    store.insert_many("poll_venues", [{"poll_id": poll_id, "venue": "polymarket",
                                       "ok": venue_ok, "n_quotes": len(rows),
                                       "latency_ms": latency_ms}])
    store.insert_many("quotes", [{**r, "poll_id": poll_id} for r in rows])
    process_poll(cfg, store, poll_id, "whale", cands,
                 {"polymarket": {"ok": venue_ok, "n": len(rows),
                                 "latency_ms": latency_ms}},
                 now=now)
    return poll_id


def _exit(cfg, store, con, bet, ctx, open_pos, lookup, books, venue_ok, now, res,
          ranked, tokens, volumes, elite_cut=None, all_pos=None,
          cache_partial=False) -> None:
    """§9 for one open bet. `exit_reason` returns None post-start (a bet is
    held to resolution then), so there is nothing extra to re-check here. A V6
    bet is re-read against THIS cycle's market — the books just fetched and the
    volume the signals above used (§7 V6, 2026-09-04) — and a V5 bet against
    this cycle's elite cutoff for its own bucket (§7 V5).

    The holdings `left` test reads two facts off the tape (2026-09-06): what
    the venue says the whales hold NOW, and the most it ever showed them
    holding SINCE THIS BET WAS BOOKED — never the prints size, which is a
    different basis."""
    if ctx is None:
        return
    cid = ctx["condition_id"]
    decision = whale_exit.exit_reason(
        ctx, open_pos, lookup, now, cfg, ranked,
        holdings=whale_holdings.for_bet(cfg, con, ctx, now, res),
        holdings_base=whale_holdings.baseline_for_bet(cfg, con, ctx, bet["created_at"]),
        all_positions=all_pos, cache_partial=cache_partial,
        market_ctx={"volume": whale_dominance.market_volume(volumes, tokens, cid),
                    "elite": elite_cut,
                    "ask": {s: t.ask for s, t in _touch(books, tokens, cid).items()}})
    if decision is None:
        return
    bid, depth = best_bid(books.get(ctx["token"]))
    if bid is None or depth < Decimal(str(cfg["min_executable_usd"])):
        note_no_bid(store, bet, decision, bid, depth, venue_ok, now)
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
