"""L14 whale-consensus lane: Gamma condition_id -> (sport, market_type,
start_utc, tokens) mapping. Split out of arb/whale_backfill.py on size alone
(mapper fix pass 2 2026-09-04) — same file, same rules: public GETs, Decimal
at the store boundary, nothing here can reach data/arb.sqlite.
"""
from __future__ import annotations

import json
import re

from arb import whale_nonsports, whale_ns_map
from arb.whale_resolve import record_resolution
from arb.whale_tape import (GAMMA_IDS_PER_CALL, SPORTS_MARKET_TYPE, _call,
                            _gamma_url, _set_meta, log)
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
#
# `no-root:` is the THIRD marker and the second permanent one (fix pass 2,
# review C1): a market whose slug says it is one of Polymarket's mechanical
# price series can never be rooted, whatever Gamma answers, so re-asking is
# pure waste — 3,669 `*-updown-*` rows sit in this queue on the live tape and
# `unmappable:` would offer every one of them again tomorrow. Permanent in the
# `absent:` sense: only `whale.nonsports.exclude_slug_patterns` changing could
# make one of these rows interesting again, and that is a registered decision,
# not a daily accident.
REVISIT_SQL = """
    SELECT m.condition_id FROM markets m
     LEFT JOIN meta u ON u.key = 'unmappable:' || m.condition_id
     LEFT JOIN meta a ON a.key = 'absent:' || m.condition_id
     LEFT JOIN meta n ON n.key = 'no-root:' || m.condition_id
     WHERE (m.sport IS NULL OR m.market_type IS NULL OR m.start_utc IS NULL)
       AND a.value IS NULL AND n.value IS NULL
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
    res.setdefault("no_root", 0)
    for i in range(0, len(todo), GAMMA_IDS_PER_CALL):
        chunk = todo[i:i + GAMMA_IDS_PER_CALL]
        found = _gamma_fetch(cfg, bucket, get, chunk, res)
        missing = [c for c in chunk if c not in found]
        if missing:
            closed = _gamma_fetch(cfg, bucket, get, missing, res, closed=True)
            for cond, m in closed.items():
                record_resolution(con, cond, m, now, "mapper")
            found.update(closed)
        # One `_sport_of` pass decides which rows the sport slugs already
        # root; only the rest are the non-sports half's business, and passing
        # that set (never `prefixes`) is what keeps arb/whale_ns_map.py free
        # of any import back into this module.
        unrooted = {c for c, m in found.items()
                    if _sport_of(str(m.get("slug") or ""), prefixes) is None}
        no_root = whale_ns_map.no_root(cfg, found, unrooted)
        roots = whale_ns_map.event_roots(cfg, bucket, get, found,
                                         unrooted - no_root, res)
        rows = [_gamma_row(c, found.get(c), prefixes, now) for c in chunk]
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
        # M4 (fix pass 2): "existing non-NULL wins" above is right for a sports
        # row and wrong for one the rooting helper roots NON-sports. The
        # pre-4b3bcc9 mapper wrote `gameStartTime` as `start_utc` for every row
        # it touched, and a `sportsMarketType` market_type with it, so a
        # politics row can be carrying a kickoff — and COALESCE would keep it
        # forever. For a non-sports root the helper's values (the market's own
        # endDate, LADDER/BINARY off the event's negRisk) ARE the authority.
        # I1 (fix pass 3): scoped to rows that are the helper's to write — no
        # sport at all, or already THIS non-sports root. Unconditionally it
        # rewrote `baseball_mlb` to `politics` on a multi-tagged event (a row
        # with a known sport but a NULL market_type is in REVISIT_SQL, and
        # Gamma's slug for it need not carry a league prefix) and replaced a
        # real kickoff with an endDate, which `first_sight_wins` protects.
        # The `sport=?` in the WHERE is idempotent re-application, not a
        # retag: a root, once set, never changes — a market's event getting
        # re-tagged on a later Gamma answer does not re-root an already-
        # rooted row (minor, fix pass 4).
        if roots:
            con.executemany("UPDATE markets SET sport=?, market_type=?, start_utc=? "
                            "WHERE condition_id=? AND (sport IS NULL OR sport=?)",
                            [(*roots[c], c, roots[c][0]) for c in roots])
        con.commit()
        for cond in missing:
            if cond not in found:
                _set_meta(con, f"absent:{cond}", today)
        stored = {r["condition_id"]: r for r in con.execute(
            f"SELECT condition_id, sport, market_type, start_utc, tokens_json "
            f"FROM markets WHERE condition_id IN ({','.join('?' * len(chunk))})",
            chunk)}
        for cond in chunk:
            row = stored[cond]
            if cond in no_root:
                res["no_root"] += 1
                _set_meta(con, f"no-root:{cond}", today)
            elif (row["sport"] and row["market_type"] and row["start_utc"]
                    and whale_nonsports.two_sided(
                        json.loads(row["tokens_json"] or "[]")) is not None):
                res["mapped"] += 1
            else:
                res["unmappable"] += 1
                log.info("whale market %s unmappable (sport=%s type=%s start=%s) "
                         "— never scored", cond, row["sport"], row["market_type"],
                         row["start_utc"])
                _set_meta(con, f"unmappable:{cond}", today)


def _gamma_row(cond, m, prefixes, now) -> tuple:
    """The non-sports root a market's own EVENT tags give it (`roots`, from
    `whale_ns_map.event_roots`) is deliberately NOT read here (fix pass 4,
    review of 5f0c2db): this function sees only the FRESH Gamma answer, never
    the row already stored, so writing a root straight into the INSERT means
    `excluded.*` can carry a non-sports `market_type`/`start_utc` for a
    condition_id whose stored `sport` is a verified sports code — and
    COALESCE picks that non-NULL `excluded` value whenever the stored field
    is still NULL, permanently. The guarded `UPDATE ... WHERE sport IS NULL
    OR sport=?` after the upsert (below) is the SOLE writer of a non-sports
    root; this function only ever leaves sport/market_type/start_utc NULL for
    a market it cannot root by slug, and COALESCE then preserves whatever
    `markets` already had."""
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
        # I3 (fix pass 2): the SAME two-sided guard the by-tag builder has
        # always applied. Colliding outcome labels share a fingerprint in the
        # lane, so they are not stored at all — `[]` leaves whatever an earlier
        # call found intact (the upsert's own NULLIF guard).
        tokens = [list(p) for p in (whale_nonsports._tokens(m) or ())]
    # `volume24hr` is a verified market-level field of this same Gamma answer
    # (data/verified_facts.md 2026-09-04) — free here, and the only source of a
    # 24h volume for a market the watch lane never quotes (non-sports roots).
    volume = None if not m or m.get("volume24hr") is None else str(m["volume24hr"])
    return (cond, sport, mtype, None, start, None, json.dumps(tokens),
            str(m.get("slug") or "") if m else None, now.isoformat(), now.isoformat(),
            volume)


# Fix pass 4 (controller, live tape read-only census 2026-09-05 over every
# `markets.slug`): an INEXACT prefix match (`head` merely STARTS WITH the
# key, `head != key`) is a genuine league code only when the slug also
# carries a `YYYY-MM-DD` date — every inexact match WITHOUT one, grouped by
# head, was a false positive: israel 38, china 6, israeli 4, nflx 3
# (Netflix), denzel 1. Every inexact match WITH one was a real league code:
# chi1 106, chi2 75, nor2 71, cricipl 66, argcopa 55, nbasl 49, bra3 48,
# crichundred 29, mlbb 26, plus ~25 more cric* families. EXACT head matches
# (`nba-playoffs-...`, `ufc-319-...`: 65 rows) never carry a date and are
# unaffected either way.
_DATED_SLUG = re.compile(r"\d{4}-\d{2}-\d{2}")


def _sport_of(slug: str, prefixes, require_date_for_inexact: bool = True) -> str | None:
    """Polymarket sports slugs lead with a league code: `kbo-lot-kt-2026-08-09`.
    Longest configured prefix wins, so one `cric` key covers crict20max,
    cricjcl, cricodc ... Unknown code -> None -> the market is never scored.
    An INEXACT match (the head only STARTS WITH the key) additionally
    requires a date in the slug — see `_DATED_SLUG` above.

    `require_date_for_inexact=False` recovers the PRE-fix-pass-4 reading (no
    date required at all) — `arb/whale_migrate.py`'s repair uses it to find
    exactly the rows the fix now roots differently, so the matching rule
    lives in this one function rather than being re-typed for comparison."""
    head = slug.split("-")[0].lower()
    for key in sorted(prefixes, key=len, reverse=True):
        if head == key or (head.startswith(key) and
                            (not require_date_for_inexact or _DATED_SLUG.search(slug))):
            return prefixes[key]
    return None
