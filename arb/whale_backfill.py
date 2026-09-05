"""L14 collector, part two: whale discovery and cold-start backfill.

Split out of arb/whale_tape.py on size alone (modules stay under ~300 lines);
same file, same rules — public GETs, Decimal at the store boundary, and
nothing here can reach data/arb.sqlite.
"""
from __future__ import annotations

from decimal import Decimal

from arb.venues.base import get_json
from arb import whale_migrate, whale_ns_map
from arb.whale_map import MAP_SQL, REVISIT_SQL, _local_remap, _map_markets, _sport_of  # noqa: F401
from arb.whale_rows import _keep
from arb.whale_tape import (DATA_API, LEADERBOARD_PAGE, _backoff_active, _bucket,
                            _call, _meta, _set_backoff, _set_meta, log)
from arb.models import q6
# resolve_sweep/SWEEP_SQL live in arb/whale_sweep.py (size, V4 fix pass
# 2026-09-04); Gamma mapping (_map_markets/_gamma_row/_local_remap/
# MAP_SQL/REVISIT_SQL) lives in arb/whale_map.py (size, mapper fix pass 2
# 2026-09-04) — imported here for `map_markets`'s own use, and `_sport_of`
# re-exported for backward compat (tests, callers reach it as
# `whale_backfill._sport_of`).


# The backfill queue, FIFO by discovery time WITHIN the relevant wallets
# (retention amendment 2026-09-04, proposal §2). With discovery reading the
# whole platform, `wallets` grows far faster than `wallets_per_cycle` drains
# it, and a queue that never drains stops being FIFO and becomes "whoever was
# discovered first" — a silent selection on top of discovery. Relevance is
# "has printed in a market with a mapped root", never anything about edge.
#
# A wallet seeded by another source (the leaderboard) is exempt: it has no
# prints yet BECAUSE backfill is what fetches them, and judging it on prints it
# cannot have would drop exactly the wallets we picked for skill. Under today's
# discovery door — which already requires a mapped root — every `source='tape'`
# wallet satisfies the EXISTS by construction, so this is the invariant that
# keeps the queue honest if that door is ever widened, not a filter that bites
# now. Rule 9 does not reach here: this is the tape, not the ledger.
RELEVANT_WALLETS_SQL = """
    SELECT w.proxy_wallet FROM wallets w
     WHERE w.backfilled_at IS NULL
       AND (w.source <> 'tape'
            OR EXISTS (SELECT 1 FROM prints p
                        JOIN markets m ON m.condition_id = p.condition_id
                       WHERE p.proxy_wallet = w.proxy_wallet AND m.sport IS NOT NULL))
     ORDER BY w.first_seen, w.proxy_wallet
     LIMIT ?"""


def _add_wallets(con, pairs, now, source) -> int:
    """New wallets only: a wallet's source is whichever lane saw it first."""
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO wallets (proxy_wallet, pseudonym, first_seen, source) "
        "VALUES (?,?,?,?)", [(w, p, now.isoformat(), source) for w, p in pairs])
    con.commit()
    return con.total_changes - before


def snapshot_leaderboard(cfg, con, now, get=get_json) -> int:
    """Seed discovery from the public leaderboard: every window in
    `leaderboard.time_periods` (DAY/WEEK/MONTH/ALL since 2026-09-04), each at
    most once per UTC day. Returns the number of wallets it added — the source
    never enters a score, but membership in the LATEST snapshot is what the V4
    variant reads (§7). One `now` for the whole run, so the windows of one day
    share one `snapshot_at` and "the latest snapshot" is a single set.

    A failed call sets the shared backoff and stops the run there; the windows
    that already landed are claimed, so the same-day retry picks up where it
    stopped instead of re-fetching them.
    """
    lb = cfg["whale"]["leaderboard"]
    if not lb["enabled"] or _backoff_active(con, now, "backfill"):
        return 0
    res, bucket, added = {}, _bucket(cfg), 0
    for period in lb["time_periods"]:
        n, ok = _snapshot_window(cfg, con, now, get, bucket, res, str(period))
        added += n
        if not ok:
            break
    return added


def _snapshot_window(cfg, con, now, get, bucket, res, period) -> tuple[int, bool]:
    """One leaderboard window. The day is claimed PER WINDOW right after page 0
    LANDS, not before it is fetched: a failure getting page 0 itself stores
    nothing, so it must not burn the day — a same-day retry should still get a
    full run of that window. Claiming it there (rather than after every page)
    is still the mid-page guard: a failure on page 1+ leaves page 0's rows
    stored and the window already claimed, so a same-day retry can't write a
    second copy of ranks 1..50.
    # ponytail: costs one day's seed for that window on a page-1+ failure; the
    # tape discovers wallets anyway, so a transaction spanning every page is
    # not worth it. Per-window keys (2026-09-04) mean a mid-run failure costs
    # only the window it happened in, not the ones already stored.
    """
    lb = cfg["whale"]["leaderboard"]
    day, key = now.strftime("%Y-%m-%d"), f"leaderboard_day:{period}"
    if _meta(con, key) == day:
        return 0, True
    added = 0
    for page in range(int(lb["pages"])):
        url = (f"{DATA_API}/v1/leaderboard?category={lb['category']}"
               f"&timePeriod={period}&orderBy={lb['order_by']}"
               f"&limit={LEADERBOARD_PAGE}&offset={page * LEADERBOARD_PAGE}")
        try:
            body = _call(bucket, get, url, cfg, res)
        except Exception:
            _set_backoff(con, cfg, now, "backfill")
            return added, False
        con.executemany(
            "INSERT INTO leaderboard_snapshots (snapshot_at, category, time_period,"
            " order_by, rank, proxy_wallet, user_name, vol, pnl) VALUES (?,?,?,?,?,?,?,?,?)",
            [(now.isoformat(), str(lb["category"]), period,
              str(lb["order_by"]), int(r.get("rank") or 0), str(r.get("proxyWallet") or ""),
              r.get("userName"), str(q6(Decimal(str(r.get("vol") or 0)))),
              str(q6(Decimal(str(r.get("pnl") or 0))))) for r in body])
        con.commit()
        if page == 0:
            _set_meta(con, key, day)
        added += _add_wallets(con, [(str(r.get("proxyWallet") or ""), r.get("userName"))
                                    for r in body], now, "leaderboard")
        if len(body) < LEADERBOARD_PAGE:
            break
    return added, True


def discover_wallets(cfg, con, now) -> int:
    """A wallet becomes a candidate whale once its cumulative PREGAME BUY
    notional in markets with a MAPPED ROOT clears discovery_min_notional_usd
    (spec §5 step 3). Summed in Decimal, never in SQL floats (rule 5).

    Retention amendment 2026-09-04: the door keeps its $1,000 value but is now
    evaluated over the whole platform, because `prints` is no longer restricted
    to the markets a cycle polled. `m.sport IS NOT NULL` is what makes that
    safe: a market the mapper could give no root — the unfollowable 5-minute
    crypto series, where churn is cheapest — counts for nothing, so it cannot
    manufacture a whale. `start_utc IS NOT NULL` alone would not do it: Gamma
    answers `gameStartTime` for non-sports markets too (weather, live probe
    2026-09-04)."""
    floor = Decimal(str(cfg["whale"]["discovery_min_notional_usd"]))
    totals: dict[str, list] = {}
    for wallet, pseudonym, notional in con.execute(
            "SELECT p.proxy_wallet, p.pseudonym, p.notional_usd FROM prints p "
            "JOIN markets m ON m.condition_id = p.condition_id "
            "WHERE p.side='BUY' AND m.start_utc IS NOT NULL AND m.sport IS NOT NULL "
            "AND p.ts < CAST(strftime('%s', m.start_utc) AS INTEGER)"):
        row = totals.setdefault(wallet, [Decimal(0), pseudonym])
        row[0] += Decimal(notional)
    return _add_wallets(con, [(w, v[1]) for w, v in totals.items() if v[0] >= floor],
                        now, "tape")


def backfill(cfg, con, now, get=get_json) -> dict:
    """Cold start (spec §5 step 4): per-wallet /activity?type=TRADE pages into
    `prints`, FIFO by discovery time. Market mapping is its OWN bounded step
    now (`map_markets`, whale-backfill fix pass 2026-09-04) — every print this
    writes carries its condition_id whether or not the market ever maps
    (`_keep(..., ids=None)`), so `prints` is itself the durable queue mapping
    reads from; folding mapping in here used to mean walking every
    condition_id this cycle's wallets had EVER touched (thousands, 2 ids a
    Gamma call), which is what blew the child's 900s cap.

    Retention (2026-09-05, `whale.retention.backfill_compact`, default true):
    the historical wallet pages this fetches were being stored at full
    fidelity — ~1.3 GB/day over the ~2,700 wallets still queued, most of it
    raw_json, which nothing downstream reads (scoring: ts/price/size/side/
    outcome_index/condition_id/asset; hygiene: price/size/side/ts). `_keep`'s
    `compact=` NULLs raw_json alone; title and every other column are kept
    exactly as fetched, same as before. `false` restores the old behaviour."""
    w = cfg["whale"]
    bf = w["backfill"]
    compact = bool(w["retention"]["backfill_compact"])
    res = {"wallets": 0, "rows": 0, "dupes": 0, "bad_rows": 0, "calls": 0,
           "status_counts": {}, "status": "ok"}
    if _backoff_active(con, now, "backfill"):
        res["status"] = "backoff"
        return res
    wallets = [r[0] for r in con.execute(RELEVANT_WALLETS_SQL,
                                         (int(bf["wallets_per_cycle"]),))]
    bucket = _bucket(cfg)
    size = int(bf["page_size"])
    try:
        for wallet in wallets:
            for page in range(int(bf["pages_per_wallet"])):
                body = _call(bucket, get, f"{DATA_API}/activity?user={wallet}&type=TRADE"
                             f"&limit={size}&offset={page * size}", cfg, res)
                _keep(con, body, None, now, res, source="activity", compact=compact)
                if len(body) < size:
                    break
            con.execute("UPDATE wallets SET backfilled_at=? WHERE proxy_wallet=?",
                        (now.isoformat(), wallet))
            con.commit()
            res["wallets"] += 1
    except Exception as e:
        log.warning("whale backfill aborted after %d call(s): %s", res["calls"], e)
        _set_backoff(con, cfg, now, "backfill")
        res["status"] = "error"
    return res


def _queue(con, limit, today) -> tuple[list[str], list[str]]:
    """I1 budget split (mapper fix pass 2, 2026-09-04): MAP_SQL used to take
    the WHOLE `map_per_cycle` limit and REVISIT_SQL only whatever was left,
    so a sustained inflow of new ids starved the revisit queue indefinitely —
    the same unbounded-backlog class 2c74fff was fixing for revisit itself.
    Each queue gets a floor of `limit // 2`; new ids are drawn first, then
    revisit gets the rest, then whatever revisit still did not use (it ran
    dry) flows back to new ids, which have already shown there is more
    supply there. No new config key — derived from `map_per_cycle`."""
    # M3 (fix pass 2, note only): new ids are drawn FIRST, so a large new-id
    # queue can hold revisit down to its `limit // 2` floor — and the
    # historical-root lookups only ever run on revisit rows. The floor is what
    # bounds the starvation; left as is deliberately, no change.
    floor = limit // 2
    new_ids = [r[0] for r in con.execute(MAP_SQL, (floor,))]
    revisit_budget = limit - len(new_ids)
    revisit = [r[0] for r in con.execute(REVISIT_SQL, (today, revisit_budget))]
    short = revisit_budget - len(revisit)
    if short > 0:
        new_ids = [r[0] for r in con.execute(MAP_SQL, (len(new_ids) + short,))]
    return new_ids, revisit


def map_markets(cfg, con, now, get=get_json) -> dict:
    """Market mapping (spec §5 step 4 continued), its own step and its own
    budget: `whale.backfill.map_per_cycle` a cycle, split between new
    condition_ids (MAP_SQL) and existing rows still missing a field
    (REVISIT_SQL) by `_queue` (I1). A revisit row already carries its OWN
    slug, so `_local_remap` fills `sport` from it directly — no Gamma call —
    before whatever still needs market_type/start_utc goes to Gamma."""
    w = cfg["whale"]
    # `root_calls` belongs in the shape THIS returns (M5, fix pass 2), not
    # only in whatever dict `_map_markets` happens to have added it to: the
    # child's call budget against max_child_runtime_secs is read off here.
    # `unrooted`/`no_root` are in the INITIAL shape, not added on the way past
    # (minor, fix pass 3): the backoff return used to omit them, so a reader of
    # the ops line could not tell zero from "that step never ran". `no_root` is
    # its own counter — a mechanical price series is not `unmappable` (Gamma
    # answered fine, and the verdict is permanent), and conflating them hid
    # both numbers.
    res = {"calls": 0, "mapped": 0, "unmappable": 0, "root_calls": 0,
           "unrooted": 0, "no_root": 0, "status_counts": {}, "status": "ok"}
    if _backoff_active(con, now, "backfill"):
        res["status"] = "backoff"
        return res
    # C1 (fix pass 2): the exclusion reaches backwards first — a row rooted
    # before the patterns existed is still scored and still priced until its
    # root is cleared. Self-terminating and free (tape only), so it runs every
    # cycle without a budget key.
    res["unrooted"] = whale_ns_map.unroot_mechanical(cfg, con, now)
    # Fix pass 4 repair: one-shot and marker-gated (arb/whale_migrate.py), so
    # this costs one `meta` lookup on every cycle after the first that finds
    # anything to reset.
    whale_migrate.repair_slug_prefix_mismap(cfg, con)
    limit = int(w["backfill"]["map_per_cycle"])
    today = now.strftime("%Y-%m-%d")
    new_ids, revisit = _queue(con, limit, today)
    todo = new_ids + _local_remap(con, revisit, w["slug_sport_prefixes"], now, res)
    if todo:
        bucket = _bucket(cfg)
        try:
            _map_markets(cfg, con, todo, now, bucket, get, res)
        except Exception as e:
            log.warning("whale mapping aborted after %d call(s): %s", res["calls"], e)
            _set_backoff(con, cfg, now, "backfill")
            res["status"] = "error"
    # KNOWN_ISSUES #36 census counter — logged here, after BOTH paths above,
    # regardless of whether `_map_markets` ran (fix pass 6, review of
    # ed390cf): `_local_remap` alone can set `res["inexact_prefix"]` (the
    # Gamma-free path, every revisit row resolved from its own stored slug),
    # and the old call site inside `_map_markets` never fired on it.
    if res.get("inexact_prefix"):
        log.info("whale mapping: %d row(s) via inexact prefix", res["inexact_prefix"])
    return res
