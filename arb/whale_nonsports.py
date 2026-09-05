"""L14 the non-sports market universe: Gamma events BY TAG -> the tape's
`markets` (amendment 2026-09-04, proposal §1 / design §5 step 1b).

Its own module rather than a section of `arb/whale_universe.py`, which reads
the LEDGER's watch quotes and must stay the one place that does. Same rules as
every other collector file: rule 1 (public, unauthenticated GETs only), rule 3
(every Gamma field below is a dated row in data/verified_facts.md), rule 4
(every threshold from `cfg["whale"]["nonsports"]`), rule 5 (the volume floor
compares Decimals), rule 9 (this file can only write the tape).

Two halves, and they never run together:

* `nonsports_universe` makes the Gamma calls and runs inside `whale-backfill`
  (cadence 600 s) — NEVER in the 60 s lane cycle, and never off the watch
  lane, which stays sports-only.
* `tape_universe` is the pure tape read the 60 s cycle uses, so a lane cycle
  costs no extra Gamma call for its non-sports half.

Ordering parameters are deliberately not used: `order`/`ascending` were sent
in the 2026-09-04 census probe and their effect was NOT verified, so every
filter here is client-side (`data/verified_facts.md`).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from arb.venues.base import get_json
from arb.whale_tape import (GAMMA, _backoff_active, _bucket, _call, _json_list,
                            _set_backoff, log)
from arb.whale_universe import MARKET_COLS, Market, persist_markets

# Endpoint shape, not a tunable: `limit=100` is the page the 2026-09-04 census
# was taken at, and `offset = page * 100` is the paging verified the same day.
EVENTS_PAGE = 100

# The lane's own half of the universe. Epoch comparison through strftime, the
# idiom whale_sweep.SWEEP_SQL already uses on this column — never a string
# compare, because `start_utc` is whatever ISO form its source wrote.
TAPE_SQL = f"""
    SELECT {MARKET_COLS} FROM markets
     WHERE sport IN ({{roots}}) AND start_utc IS NOT NULL AND market_type IS NOT NULL
       AND CAST(strftime('%s', start_utc) AS INTEGER) BETWEEN ? AND ?
     ORDER BY CAST(strftime('%s', start_utc) AS INTEGER), condition_id"""


def _when(value) -> datetime | None:
    """A Gamma timestamp as an aware UTC datetime; anything unparseable is
    None, and every caller reads None as "cannot measure" -> excluded."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _volume(value) -> Decimal | None:
    """`volume24hr` is present as a key on every nested market but is often
    null (measured 2026-09-04: 254 of 485 open crypto markets had a value).
    Null is NOT an unknown to be admitted — no measured flow fails the floor."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def root_of_event(cfg, event) -> str | None:
    """The event's own `tags` array -> the configured root.

    Events are multi-tagged and the tags overlap (verified 2026-09-04: one
    event is tagged crypto AND politics AND trump-presidency), so the root must
    not depend on which tag's page surfaced the market or the same market would
    land in a different root on a different refresh. The FIRST configured tag
    present wins, and `whale.nonsports.tags` is an ordered map for exactly that
    reason. An event carrying none of them is not ours."""
    tags = {str(t.get("slug") or "") for t in (event.get("tags") or [])}
    for tag, root in cfg["whale"]["nonsports"]["tags"].items():
        if tag in tags:
            return root
    return None


def _market_type(event) -> str:
    """design §5 step 1b: LADDER when the EVENT's negRisk is true (the
    temperature and strike ladders), else BINARY. Gamma OMITS `negRisk` when
    it is false — measured 2026-09-05 over 2,822 tag-page events, `negRisk` is
    present on 970 and `enableNegRisk` on all 2,822, and the two never
    disagree — so a missing key reads as false, which is what `.get` does."""
    return "LADDER" if event.get("negRisk") else "BINARY"


def _lifetime(event, market, end):
    """`end` minus the LATEST of the market's own `startDate`, its `createdAt`
    and the event's `startDate`: the shortest of the measurable spans, which
    is the reading that admits fewer markets. None = no measurable start, and
    every caller reads that as excluded rather than assumed long-lived."""
    starts = [s for s in (_when(market.get("startDate")), _when(market.get("createdAt")),
                          _when(event.get("startDate"))) if s is not None]
    return end - max(starts) if starts else None


def root_row(cfg, event, market) -> tuple | None:
    """`(root, market_type, start_utc)` for a HISTORICAL non-sports market —
    one the mapper (`arb/whale_map.py`) fetched by condition_id and could not
    root from a sport slug — or None when the market is not ours.

    The same tag map, the same `market_type` convention and the same
    `start_utc := the MARKET's own endDate` `markets_of` applies, so a
    politics market a whale traded last month roots exactly as the live
    builder would have rooted it (Opus review C2: without this, every
    historical non-sports market stayed `sport=NULL` and
    `whale_score.positions_from_prints` dropped it, so no politics whale
    could ever be scored).

    The filters that are about BOOKING are deliberately NOT applied: `closed`,
    the `volume24hr` floor and the `max_days` horizon all ask "can the lane
    trade this now", which a resolved market cannot and need not answer — its
    only use is scoring a whale's past. The lifetime floor IS applied: it is a
    statement about the market, not about the moment.
    """
    root, end = root_of_event(cfg, event), _when(market.get("endDate"))
    if root is None or end is None:
        return None
    life = _lifetime(event, market, end)
    if life is None or life < timedelta(
            hours=float(cfg["whale"]["nonsports"]["min_lifetime_hours"])):
        return None
    return root, _market_type(event), end.isoformat()


def _tokens(market) -> tuple | None:
    """((token_id, label), ...) for a two-sided market, else None.

    The lane BUYS one token and `_touch` reads outcome indices 0 and 1, so
    anything but exactly two distinguishable outcomes is not bookable here —
    skipped rather than booked under a fingerprint two tokens would share
    (`whale_universe._labels`' rule, on the shape Gamma sends for events)."""
    pairs = tuple(zip(_json_list(market.get("clobTokenIds")),
                      _json_list(market.get("outcomes"))))
    if len(pairs) != 2 or len({p[1] for p in pairs}) != 2 or not all(p[0] for p in pairs):
        return None
    return tuple((str(t), str(o)) for t, o in pairs)


def markets_of(cfg, event, now) -> list[Market]:
    """The nested markets of ONE Gamma event that belong in the universe.

    Four filters, all client-side (design §5 step 1b): the market is open; its
    `volume24hr` clears `min_volume_24h_usd`; its lifetime clears
    `min_lifetime_hours`; and its own `endDate` — never the event's, which
    differs from it (verified 2026-09-04) — lies inside
    `[now, now + whale.nonsports.max_days]` — the LANE's own horizon key,
    shipped at the ledger's shared `nonsports_max_days` (7) and read by
    `arb/tracker.py` for WHALE rows, so the lane never prices a market it
    could not book.

    Lifetime is `_lifetime` — shared with `root_row`, which the mapper uses to
    root the same kind of market from its history rather than from a tag page.
    """
    ns = cfg["whale"]["nonsports"]
    root = root_of_event(cfg, event)
    if root is None:
        return []
    floor = Decimal(str(ns["min_volume_24h_usd"]))
    life = timedelta(hours=float(ns["min_lifetime_hours"]))
    horizon = now + timedelta(days=int(ns["max_days"]))
    market_type = _market_type(event)
    out = []
    for m in (event.get("markets") or []):
        cond, end = str(m.get("conditionId") or ""), _when(m.get("endDate"))
        volume = _volume(m.get("volume24hr"))
        lifetime = _lifetime(event, m, end) if end is not None else None
        tokens = _tokens(m)
        if (not cond or m.get("closed") or end is None or lifetime is None
                or tokens is None or volume is None or volume < floor
                or not (now <= end <= horizon) or lifetime < life):
            continue
        out.append(Market(
            condition_id=cond, sport=root, market_type=market_type, line=None,
            # §7 item 5: `start_utc` IS the resolution deadline, so the existing
            # pregame gate reads "before resolution" and min_secs_to_start reads
            # "not inside the last ten minutes" — no scoring code changes.
            start_utc=end.isoformat(),
            event_key=f"pm:{event.get('slug') or cond}",
            tokens=tokens, volume_24h=str(volume)))
    return out


def nonsports_universe(cfg, con, now, get=get_json) -> dict:
    """Walk `whale.nonsports.tags` and upsert what survives `markets_of`.

    Bounded by construction: `pages` pages of `EVENTS_PAGE` events per tag,
    paced by the same shared token bucket as every other whale call, stopping a
    tag early on a short page. At 3 tags x 3 pages that is 9 calls per refresh.
    """
    ns = cfg["whale"]["nonsports"]
    res = {"calls": 0, "events": 0, "markets": 0, "status_counts": {}, "status": "ok"}
    if not ns["enabled"]:
        res["status"] = "disabled"
        return res
    if _backoff_active(con, now):
        res["status"] = "backoff"
        return res
    bucket, found = _bucket(cfg), {}
    try:
        for tag in ns["tags"]:
            for page in range(int(ns["pages"])):
                body = _call(bucket, get,
                             f"{GAMMA}/events?tag_slug={tag}&closed=false"
                             f"&limit={EVENTS_PAGE}&offset={page * EVENTS_PAGE}",
                             cfg, res)
                res["events"] += len(body)
                for event in body:
                    for market in markets_of(cfg, event, now):
                        # `root_of_event` is a function of the event's own tags,
                        # so a market listed under two of our tags resolves to
                        # the same root either way and first-seen is arbitrary
                        # only in ordering.
                        found.setdefault(market.condition_id, market)
                if len(body) < EVENTS_PAGE:
                    break
    except Exception as e:  # noqa: BLE001 — a venue outage pauses the refresh
        log.warning("whale non-sports universe aborted after %d call(s): %s",
                    res["calls"], e)
        _set_backoff(con, cfg, now)
        res["status"] = "error"
    if found:
        # first_sight_wins: an edited Gamma endDate must never rewrite a stored
        # `start_utc` (look-ahead), and a market already mapped to a real sport
        # must never be re-labelled by a tag page that happens to list it.
        persist_markets(con, list(found.values()), now, first_sight_wins=True)
    res["markets"] = len(found)
    log.info("whale non-sports universe: %s", res)
    return res


def tape_universe(con, cfg, now) -> list[Market]:
    """The 60 s cycle's non-sports half: non-sports-rooted `markets` rows whose
    resolution deadline is still ahead of us and inside
    `whale.nonsports.max_days` — the same horizon `arb/tracker.py` applies to a
    WHALE row at booking, so the lane never prices a market it could not book —
    and whose measured 24h volume clears `min_volume_24h_usd`.

    The volume floor is applied HERE and not only in `markets_of` (amendment
    2026-09-05): `nonsports_universe` is no longer the only writer of a
    non-sports `markets` row. `whale_map._event_roots` roots a market from its
    event's tags so a whale's HISTORICAL politics positions can be scored, and
    it deliberately applies no volume floor — a resolved market's `volume24hr`
    is no reason to refuse to score the whale who traded it. Most such rows
    are in the past and this query's lower bound already excludes them, but
    not all: measured on the live tape 2026-09-05, the first 100-row sample of
    the revisit queue held a politics market with a future `endDate` and a
    NULL `volume24hr`. This read is the one place the LANE's universe is
    decided, so the registered floor belongs here — a null volume failing it
    exactly as it does in `markets_of` (design §5 step 1b).
    """
    ns = cfg["whale"]["nonsports"]
    roots = sorted(set(ns["tags"].values()))
    if not ns["enabled"] or not roots:
        return []
    hi = now + timedelta(days=int(ns["max_days"]))
    floor = Decimal(str(ns["min_volume_24h_usd"]))
    rows = con.execute(TAPE_SQL.format(roots=",".join("?" * len(roots))),
                       (*roots, int(now.timestamp()), int(hi.timestamp())))
    out = []
    for r in rows:
        volume = _volume(r["volume_24h"])
        if volume is None or volume < floor:
            continue
        out.append(Market(condition_id=r["condition_id"], sport=r["sport"],
                          market_type=r["market_type"], line=r["line"],
                          start_utc=r["start_utc"], event_key=r["event_key"],
                          tokens=tuple((t[0], t[1])
                                       for t in json.loads(r["tokens_json"] or "[]")),
                          volume_24h=r["volume_24h"]))
    return out


def seed_volumes(volumes, markets) -> dict:
    """Fill V6's denominator for a market the watch lane never quotes.

    The lane builds `volumes` from the ledger's own watch quotes, which cover
    Polymarket SPORTS only — a non-sports market would then read `None`, which
    `whale_dominance.point` treats as `dominance.min_volume_usd` ($5,000) and a
    $20M/day politics market would come out maximally dominant. `setdefault`
    keeps the ledger authoritative wherever it has an answer; this only fills
    the gap, from the Gamma figure `markets.volume_24h` already stores."""
    for m in markets:
        if m.volume_24h is not None:
            for token, _ in m.tokens:
                volumes.setdefault(f"{m.condition_id}:{token}", Decimal(m.volume_24h))
    return volumes
