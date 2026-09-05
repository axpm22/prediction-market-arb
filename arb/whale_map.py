"""L14 whale-consensus lane: Gamma condition_id -> (sport, market_type,
start_utc, tokens) mapping. Split out of arb/whale_backfill.py on size alone
(mapper fix pass 2 2026-09-04) — same file, same rules: public GETs, Decimal
at the store boundary, nothing here can reach data/arb.sqlite.
"""
from __future__ import annotations

import json

from arb import whale_nonsports
from arb.whale_resolve import record_resolution
from arb.whale_tape import (GAMMA, GAMMA_IDS_PER_CALL, SPORTS_MARKET_TYPE, _call,
                            _gamma_url, _json_list, _set_meta, log)
from arb.whale_universe import MARKET_COLS, _iso

# New condition_ids, FIFO by first print (any deterministic order is fine —
# spec's own words): every distinct condition_id `prints` has ever seen that
# `markets` has NO row for at all. Mirrors SWEEP_SQL's shape on purpose — same
# bounded-FIFO-off-the-tape idiom, same reason (a cycle must never walk an
# unbounded backlog).
#
# The `wallets` EXISTS is the relevance guard the retention amendment
# (2026-09-04, proposal §2) had to land WITH the global print door: `prints`
# now holds every $1,000-plus trade on Polymarket, so without it this queue
# inherits every market on the platform — of the order of 300 five-minute
# crypto markets a day, forever, each spending Gamma calls to map a market no
# root will ever score. A market is worth a call once a wallet we already
# follow has traded in it; anything a universe builder brings in of its own
# accord already has its `markets` row and never enters this queue at all.
MAP_SQL = """
    SELECT p.condition_id FROM prints p
     LEFT JOIN markets m ON m.condition_id = p.condition_id
     WHERE m.condition_id IS NULL AND p.condition_id <> ''
       AND EXISTS (SELECT 1 FROM wallets w WHERE w.proxy_wallet = p.proxy_wallet)
     GROUP BY p.condition_id
     ORDER BY MIN(p.ts), p.condition_id
     LIMIT ?"""

# Existing rows still missing a field, oldest first (mapper fix pass
# 2026-09-04): the ORIGINAL sin here was `_map_markets`' INSERT OR IGNORE —
# once a condition_id had ANY `markets` row, MAP_SQL's LEFT JOIN could never
# select it again, so a row stored with sport set (e.g. by a one-off prefix
# remap) and market_type still NULL was stuck that way forever, and could
# never enter SWEEP_SQL (which requires both).
#
# The `slug IS NOT NULL` floor this query used to carry (mapper fix pass
# 2026-09-04) is GONE (legacy re-map, 2026-09-04): it read as "no slug means
# Gamma never found the market, nothing left to retry", but 12,091 `markets`
# rows the OLD mapper (pre-4b3bcc9) inserted from Gamma's default index
# alone carry slug/sport/start_utc all NULL — the floor made those rows
# PERMANENTLY unrevisitable, and a live probe found the ids DO exist on
# Gamma with `closed=true`. The `absent:` join below replaces it as the
# permanent stop: only an id `_map_markets` has actually asked TWICE
# (default pass, then closed=true) and found nowhere both times earns it, so
# a legacy slug-NULL row gets a real chance before being excluded for good.
# The `unmappable` join is `swept:`'s own idiom — a row Gamma still
# couldn't map today is skipped until tomorrow instead of spending the same
# call every cycle for nothing; `absent` is its permanent sibling, immune to
# the day-string comparison.
REVISIT_SQL = """
    SELECT m.condition_id FROM markets m
     LEFT JOIN meta u ON u.key = 'unmappable:' || m.condition_id
     LEFT JOIN meta a ON a.key = 'absent:' || m.condition_id
     WHERE (m.sport IS NULL OR m.market_type IS NULL OR m.start_utc IS NULL)
       AND a.value IS NULL
       AND (u.value IS NULL OR u.value <> ?)
     ORDER BY m.first_seen, m.condition_id
     LIMIT ?"""


def _local_remap(con, ids, prefixes, now, res) -> list[str]:
    """A revisit row's slug is already on hand, so a prefix hit costs no
    Gamma call. Drop an id from the queue only when that leaves nothing
    else missing; whatever still needs market_type/start_utc goes on."""
    todo = []
    for cond in ids:
        row = con.execute("SELECT sport, market_type, start_utc, slug FROM markets "
                          "WHERE condition_id=?", (cond,)).fetchone()
        sport = row["sport"] or _sport_of(row["slug"] or "", prefixes)
        if sport and row["market_type"] and row["start_utc"]:
            con.execute("UPDATE markets SET sport=?, last_seen=? WHERE condition_id=?",
                        (sport, now.isoformat(), cond))
            res["mapped"] += 1
        else:
            todo.append(cond)
    con.commit()
    return todo


def _event_roots(cfg, bucket, get, found, prefixes, res) -> dict:
    """{condition_id: (root, market_type, start_utc)} for the rows of ONE
    chunk that no sport slug can root — the non-sports half of the mapper
    (amendment 2026-09-05, Opus review C2).

    Until this existed, `_gamma_row` rooted a market from its slug prefix
    alone, so every HISTORICAL politics or crypto market a whale had traded
    stayed `sport=NULL`, `whale_score.positions_from_prints` dropped it as
    unmapped, and no politics whale could ever be scored — while the by-tag
    universe builder, which roots exactly these markets, only ever sees the
    OPEN ones on today's tag pages.

    Why a second call at all (rule 3, all live-probed): Gamma's
    `markets?condition_ids=` row carries no `tags` and no `category`, and its
    nested `events[0]` carries no `tags` key either (2026-09-04, re-probed
    2026-09-05) — it carries the event's `id`, and `events?id=` DOES return a
    populated `tags` array, for closed events too and with no `closed=true`
    needed. That call takes the same repeated-param form and the same silent
    20-id ceiling as `markets?condition_ids=` (2026-09-05: n=3 -> 3/3,
    n=20 -> 20/20, n=25 -> 20 rows), so it reuses `GAMMA_IDS_PER_CALL`.

    Cost: at most ONE extra call per chunk — a chunk is 20 condition_ids, so
    its distinct events never exceed the 20-id ceiling — and at most
    `whale.nonsports.root_calls_per_cycle` of them in a mapping cycle. A row
    the cap skips is not lost: it takes the ordinary `unmappable:` marker and
    comes back through REVISIT_SQL on the next day's queue.
    """
    ns = cfg["whale"]["nonsports"]
    if not ns["enabled"] or res["root_calls"] >= int(ns["root_calls_per_cycle"]):
        return {}
    want: dict = {}
    for cond, m in found.items():
        if _sport_of(str(m.get("slug") or ""), prefixes) is not None:
            continue
        event_id = ((m.get("events") or [{}])[0] or {}).get("id")
        if event_id is not None:
            want.setdefault(str(event_id), []).append(cond)
    if not want:
        return {}
    ids = list(want)[:GAMMA_IDS_PER_CALL]
    body = _call(bucket, get, f"{GAMMA}/events?"
                 + "&".join(f"id={i}" for i in ids), cfg, res)
    res["root_calls"] += 1
    out = {}
    for event in body:
        for cond in want.get(str(event.get("id")), []):
            row = whale_nonsports.root_row(cfg, event, found[cond])
            if row:
                out[cond] = row
    return out


def _gamma_fetch(cfg, bucket, get, ids, res, closed=False) -> dict:
    """One Gamma condition_ids call -> {conditionId: raw row}."""
    body = _call(bucket, get, _gamma_url(ids, closed=closed), cfg, res)
    return {str(m.get("conditionId") or ""): m for m in body}


def _map_markets(cfg, con, todo, now, bucket, get, res):
    """Gamma mapping for a bounded, ordered slice of condition_ids (already
    decided by `whale_backfill.map_markets`, or a test).

    Two Gamma passes per chunk (F0, mapper fix pass 2 2026-09-04, live probe
    dated in data/verified_facts.md): Gamma's default index omits an
    old/resolved market, so an id absent from the first (default) pass gets a
    SECOND request with `&closed=true` before it counts as not-on-Gamma —
    reuses `whale_resolve`'s own closed=true idiom (`_gamma_url`) rather than
    duplicating it. At most GAMMA_IDS_PER_CALL=20 ids a call (rule 3: verified
    live, legacy re-map 2026-09-04 — a larger batch silently drops markets);
    the retry's id list is already a subset of one
    such chunk, so it never needs its own re-chunking.

    Upserts with COALESCE, EXISTING non-NULL wins (C1, mapper fix pass 2):
    the old `COALESCE(excluded.x, x)` let a revisit call's answer overwrite a
    field an earlier, more authoritative source had already set — `sport` in
    particular, which for a market also seen by the watch lane was set from
    its own sport TAG (ground truth), not the slug-prefix heuristic
    `_gamma_row` falls back to. Filling NULLs only never loses that. `slug`
    gets the same treatment for the same reason. `tokens_json` keeps its own
    pre-existing guard: a Gamma miss (`[]`) never blanks out tokens an
    earlier call already found.

    The `unmappable:` marker (C2, mapper fix pass 2) is decided from the row
    AS STORED after the upsert, not from this call's own Gamma answer: with
    C1's COALESCE now filling NULLs only, a revisit row can already be fully
    mapped in every field but the ONE this call was fetching for, even when
    THIS call's own answer was empty — checking the local answer alone used
    to skip the marker on exactly that row (previously: `r[1] and r[2]`,
    sport+market_type only, ignoring start_utc — the same miss REVISIT_SQL
    itself checks for, so an id missing only start_utc got no marker and sat
    at the head of the revisit queue every cycle forever).

    The `absent:` marker (legacy re-map, 2026-09-04) is set only for an id
    still missing from `found` after BOTH passes — the permanent sibling of
    `unmappable:` below: REVISIT_SQL excludes it forever, not just for
    today, because two real asks (default index, then closed=true) both
    came back with nothing. This is what lets the old mapper's 12,091
    slug-NULL legacy rows re-enter the revisit queue (REVISIT_SQL's
    `slug IS NOT NULL` floor is gone) without looping on Gamma forever for
    the ones that turn out not to exist there either.

    A closed-pass Gamma row this call is about to mine for sport/start_utc
    may ALSO be a resolved market (F0's second pass is the same query
    `read_resolutions` uses) — `record_resolution` (arb/whale_resolve.py,
    shared with the resolution reader, source='mapper') persists it right
    here so the resolution sweep never spends its own call re-asking the
    same question once this market ages out of the 60s cycle's universe."""
    prefixes = cfg["whale"]["slug_sport_prefixes"]
    today = now.strftime("%Y-%m-%d")
    res.setdefault("root_calls", 0)
    for i in range(0, len(todo), GAMMA_IDS_PER_CALL):
        chunk = todo[i:i + GAMMA_IDS_PER_CALL]
        found = _gamma_fetch(cfg, bucket, get, chunk, res)
        missing = [c for c in chunk if c not in found]
        if missing:
            closed = _gamma_fetch(cfg, bucket, get, missing, res, closed=True)
            for cond, m in closed.items():
                record_resolution(con, cond, m, now, "mapper")
            found.update(closed)
        roots = _event_roots(cfg, bucket, get, found, prefixes, res)
        rows = [_gamma_row(c, found.get(c), prefixes, now, roots) for c in chunk]
        con.executemany(
            f"INSERT INTO markets ({MARKET_COLS}) VALUES ({','.join('?' * 11)}) "
            "ON CONFLICT(condition_id) DO UPDATE SET last_seen=excluded.last_seen, "
            "sport=COALESCE(sport, excluded.sport), "
            "market_type=COALESCE(market_type, excluded.market_type), "
            "start_utc=COALESCE(start_utc, excluded.start_utc), "
            "slug=COALESCE(slug, excluded.slug), "
            "volume_24h=COALESCE(excluded.volume_24h, volume_24h), "
            "tokens_json=COALESCE(NULLIF(excluded.tokens_json, '[]'), tokens_json)",
            rows)
        con.commit()
        for cond in missing:
            if cond not in found:
                _set_meta(con, f"absent:{cond}", today)
        stored = {r["condition_id"]: r for r in con.execute(
            f"SELECT condition_id, sport, market_type, start_utc FROM markets "
            f"WHERE condition_id IN ({','.join('?' * len(chunk))})", chunk)}
        for cond in chunk:
            row = stored[cond]
            if row["sport"] and row["market_type"] and row["start_utc"]:
                res["mapped"] += 1
            else:
                res["unmappable"] += 1
                log.info("whale market %s unmappable (sport=%s type=%s start=%s) "
                         "— never scored", cond, row["sport"], row["market_type"],
                         row["start_utc"])
                _set_meta(con, f"unmappable:{cond}", today)


def _gamma_row(cond, m, prefixes, now, roots=None) -> tuple:
    """`roots` is `_event_roots`' answer for this chunk: the non-sports root a
    market's own EVENT tags give it, used only where the slug gives none."""
    sport = mtype = start = None
    tokens: list = []
    if m:
        mtype = SPORTS_MARKET_TYPE.get(str(m.get("sportsMarketType") or "").lower())
        sport = _sport_of(str(m.get("slug") or ""), prefixes)
        # `gameStartTime` is a KICKOFF, and only a sports row wants one (I2,
        # fix pass). For a market this mapper cannot root, `start_utc` belongs
        # to the tag builder, which writes the Gamma `endDate` so the pregame
        # gate reads "before resolution" (§7 item 5) — and `first_sight_wins`
        # would then keep whichever landed first, so a kickoff pinned here
        # would stand forever.
        start = _iso(m.get("gameStartTime")) if sport else None
        if sport is None and roots and cond in roots:
            # ... and that is exactly what the tag builder would have written
            # for this market, computed from the same helper (amendment
            # 2026-09-05): root, LADDER/BINARY from the event's negRisk, and
            # `start_utc` = the MARKET's own endDate, never the event's.
            sport, mtype, start = roots[cond]
        tokens = [[t, o] for t, o in zip(_json_list(m.get("clobTokenIds")),
                                         _json_list(m.get("outcomes")))]
    # `volume24hr` is a verified market-level field of this same Gamma answer
    # (data/verified_facts.md 2026-09-04) — free here, and the only source of a
    # 24h volume for a market the watch lane never quotes (non-sports roots).
    volume = None if not m or m.get("volume24hr") is None else str(m["volume24hr"])
    return (cond, sport, mtype, None, start, None, json.dumps(tokens),
            str(m.get("slug") or "") if m else None, now.isoformat(), now.isoformat(),
            volume)


def _sport_of(slug: str, prefixes) -> str | None:
    """Polymarket sports slugs lead with a league code: `kbo-lot-kt-2026-08-09`.
    Longest configured prefix wins, so one `cric` key covers crict20max,
    cricjcl, cricodc ... Unknown code -> None -> the market is never scored."""
    head = slug.split("-")[0].lower()
    for key in sorted(prefixes, key=len, reverse=True):
        if head == key or head.startswith(key):
            return prefixes[key]
    return None
