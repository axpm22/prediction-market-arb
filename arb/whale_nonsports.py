"""L14 the non-sports market universe: Gamma events BY TAG -> the tape's
`markets` (amendment 2026-09-04, proposal §1 / design §5 step 1b).

Its own module rather than a section of `arb/whale_universe.py`, which reads
the LEDGER's watch quotes and must stay the one place that does. Same rules as
every other collector file: rule 1 (public, unauthenticated GETs only), rule 3
(every Gamma field below is a dated row in data/verified_facts.md), rule 4
(every threshold from `cfg["whale"]["nonsports"]`), rule 5 (the volume floor
compares Decimals), rule 9 (this file can only write the tape).

This module is the GAMMA half: it roots a market and decides whether it is
ours. `nonsports_universe` makes the tag calls and runs inside `whale-backfill`
(cadence 600 s) — NEVER in the 60 s lane cycle, and never off the watch lane,
which stays sports-only. The other half, the pure tape read the 60 s cycle uses,
is `arb/whale_ns_lane.py` (split out on size, fix pass 2 2026-09-05); nothing
in that file imports GAMMA or `_call`, which is the point of the boundary.

Ordering parameters are deliberately not used: `order`/`ascending` were sent
in the 2026-09-04 census probe and their effect was NOT verified, so every
filter here is client-side (`data/verified_facts.md`).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from arb.venues.base import get_json
from arb.whale_tape import (GAMMA, _backoff_active, _bucket, _call, _json_list,
                            _set_backoff, log)
from arb.whale_universe import Market, persist_markets

# Endpoint shape, not a tunable: `limit=100` is the page the 2026-09-04 census
# was taken at, and `offset = page * 100` is the paging verified the same day.
EVENTS_PAGE = 100

# The NEGATIVE half of the mechanical-price-series exclusion (fix pass 2,
# review C1): a slug that ends in a cadence token plus an epoch —
# `btc-updown-5m-1785140700`, `eth-updown-15m-…`, `…-1h-…`, `…-4h-…`. Anchored
# at the end so a dollar figure in a market slug (`over-12m-committed-to-…`)
# can never read as twelve minutes. A registered pattern list alone would let
# the next cadence family Gamma ships walk in before anyone edits config.
_CADENCE = re.compile(r"-(\d+)([mh])-\d+$")


@lru_cache(maxsize=64)
def _rx(pattern: str):
    """Config patterns are a short, constant list; compile each one once."""
    return re.compile(pattern)


def _patterns(cfg) -> list:
    """`exclude_slug_patterns` with `{coin}` expanded to the registered
    `coin_tokens`, longest first so `bitcoin` is never shadowed by `btc`
    (I2, fix pass 3: three patterns described a COIN family with a bare
    `[a-z]+` and excluded `will-trump-reach-50-approval-in-march` and
    `oil-price-in-london-march-3` — and `no-root:` is permanent).

    An EMPTY `coin_tokens` drops the `{coin}` patterns instead of expanding
    them (minor, fix pass 5, review of 333e264): `{coin}` -> `(?:)` — an
    empty group with no coin name to require — degenerates each of the three
    patterns into an anchor a market slug could satisfy by accident, not the
    coin-shaped exclusion the pattern was written to be."""
    ns = cfg["whale"]["nonsports"]
    patterns = ns["exclude_slug_patterns"]
    tokens = sorted((t for t in ns["coin_tokens"] if t), key=len, reverse=True)
    if not tokens:
        return [p for p in patterns if "{coin}" not in p]
    coin = "(?:%s)" % "|".join(re.escape(t) for t in tokens)
    return [p.replace("{coin}", coin) for p in patterns]


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
    reason. An event carrying none of them is not ours.

    BOTH tag shapes are accepted (fix pass 2, review I1): the live
    `events?id=` body — `tests/fixtures/whale_gamma_events_by_id.json`, probed
    2026-09-05 — sends objects carrying `slug`/`label`/`id`, but a bare string
    list must not raise here: this runs inside `whale_map._event_roots`, and an
    exception there aborts the whole mapping cycle and arms the whale-wide
    backoff, every cycle, forever."""
    tags = {t.get("slug") if isinstance(t, dict) else str(t)
            for t in (event.get("tags") or [])}
    for tag, root in cfg["whale"]["nonsports"]["tags"].items():
        if tag in tags:
            return root
    return None


def _excluded(cfg, event, market) -> bool:
    """True when this market is one of Polymarket's MECHANICAL PRICE SERIES —
    the recurring crypto up/down cadence events and the daily strike ladders
    (config `whale.nonsports.exclude_slug_patterns`; operator decision
    2026-09-05, option 1: keep the crypto root, exclude the series).

    Called from `root_row`, the ONE shared rooting helper, so the by-tag
    builder and the mapper's historical rooting cannot disagree about it. No
    other registered filter reaches this series: the cadence events read a
    23.94 h lifetime (measured 2026-09-04) so `min_lifetime_hours` cannot stop
    them, and the horizon never did either. Counts, per-pattern yields and the
    zero-collateral check on politics/geopolitics are in the config comment
    and data/verified_facts.md, dated 2026-09-05."""
    ns = cfg["whale"]["nonsports"]
    event_slug = str(event.get("slug") or "")
    slugs = [s for s in (event_slug, str(market.get("slug") or "")) if s]
    if any(_rx(p).match(s) for p in _patterns(cfg) for s in slugs):
        return True
    # The cadence token names the EVENT's own life, so it is read off the event
    # slug alone (and this series ships the same slug on both).
    return any(int(n) * (1 if unit == "h" else 1 / 60)
               < float(ns["min_lifetime_hours"])
               for n, unit in _CADENCE.findall(event_slug))


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
    statement about the market, not about the moment. So is `_excluded` (fix
    pass 2, review C1) — a mechanical price series is not a market about
    anything, whenever you ask.

    `markets_of` calls THIS, so there is exactly one rooting rule and one
    exclusion, and adding a filter here can never leave the other path behind.
    """
    root, end = root_of_event(cfg, event), _when(market.get("endDate"))
    if root is None or end is None or _excluded(cfg, event, market):
        return None
    life = _lifetime(event, market, end)
    if life is None or life < timedelta(
            hours=float(cfg["whale"]["nonsports"]["min_lifetime_hours"])):
        return None
    return root, _market_type(event), end.isoformat()


def two_sided(pairs) -> tuple | None:
    """((token_id, label), ...) for a two-sided market, else None.

    The lane BUYS one token and `_touch` reads outcome indices 0 and 1, so
    anything but exactly two distinguishable outcomes is not bookable here —
    skipped rather than booked under a fingerprint two tokens would share
    (`whale_universe._labels`' rule). Takes already-paired (token, label)
    tuples so the mapper (`whale_map._gamma_row`, which zips them itself) and
    the lane's tape read (`whale_ns_lane`, which parses them out of stored
    `tokens_json`) can apply the SAME guard the builder always did — I3, fix
    pass 2: only the builder had it, so the mapper could store colliding
    labels."""
    try:
        pairs = tuple((str(t), str(o)) for t, o in pairs)
    except (TypeError, ValueError):
        return None
    if len(pairs) != 2 or len({p[1] for p in pairs}) != 2 or not all(p[0] for p in pairs):
        return None
    return pairs


def _tokens(market) -> tuple | None:
    """`two_sided` on the shape Gamma sends for a nested event market."""
    return two_sided(zip(_json_list(market.get("clobTokenIds")),
                         _json_list(market.get("outcomes"))))


def markets_of(cfg, event, now) -> list[Market]:
    """The nested markets of ONE Gamma event that belong in the universe.

    Four filters, all client-side (design §5 step 1b): the market is open; its
    `volume24hr` clears `min_volume_24h_usd`; its lifetime clears
    `min_lifetime_hours`; and its own `endDate` — never the event's, which
    differs from it (verified 2026-09-04) — lies inside
    `[now, now + whale.nonsports.max_days]` — the LANE's own horizon key (90
    since the amendment of 2026-09-05, deliberately no longer the ledger's
    shared `nonsports_max_days`), read by `arb/tracker.py` for WHALE rows, so
    the lane never prices a market it could not book.

    Root, market_type, the lifetime floor and the mechanical-price-series
    exclusion are `root_row`'s — the one shared helper the mapper also uses to
    root this same kind of market from a whale's history rather than from a tag
    page. Only the three BOOKING filters are this function's own.
    """
    ns = cfg["whale"]["nonsports"]
    if root_of_event(cfg, event) is None:
        return []
    floor = Decimal(str(ns["min_volume_24h_usd"]))
    horizon = now + timedelta(days=int(ns["max_days"]))
    out = []
    for m in (event.get("markets") or []):
        cond, row = str(m.get("conditionId") or ""), root_row(cfg, event, m)
        volume = _volume(m.get("volume24hr"))
        tokens = _tokens(m)
        if (not cond or m.get("closed") or row is None or tokens is None
                or volume is None or volume < floor):
            continue
        root, market_type, start = row
        end = _when(start)
        if not (now <= end <= horizon):
            continue
        out.append(Market(
            condition_id=cond, sport=root, market_type=market_type, line=None,
            # §7 item 5: `start_utc` IS the resolution deadline, so the existing
            # pregame gate reads "before resolution" and min_secs_to_start reads
            # "not inside the last ten minutes" — no scoring code changes.
            start_utc=start,
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
