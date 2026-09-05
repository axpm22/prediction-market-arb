"""L14 the mapper's NON-SPORTS half: a condition_id Gamma answers for, but no
sport slug can root -> the root its own EVENT's tags give it.

Split out of `arb/whale_map.py` on size (fix pass 2, 2026-09-05) along the one
seam that file has: everything here is about the non-sports roots, and nothing
here knows anything about sports slugs — the caller decides which rows a sport
prefix already rooted and passes only the rest (`unrooted`), which is also why
this module can never import back into `whale_map`.

Same rules as every collector file: rule 1 (public, unauthenticated GETs), rule
3 (every field below is a dated row in data/verified_facts.md), rule 4 (the
budget is `whale.nonsports.root_calls_per_cycle`).
"""
from __future__ import annotations

from arb import whale_nonsports
from arb.whale_tape import GAMMA, GAMMA_IDS_PER_CALL, _call, _set_meta, log


def no_root(cfg, found, unrooted) -> set:
    """The condition_ids of this chunk that are Polymarket MECHANICAL PRICE
    SERIES — the crypto up/down cadence events and the daily strike ladders
    (fix pass 2, review C1). `whale_nonsports._excluded` is the one registered
    rule, applied here to the market's own slug and to the slug its nested
    `events[0]` carries (that answer has no tags, but it does have that much —
    probe 2026-09-05), so the exclusion costs no Gamma call at all.

    The caller marks these `no-root:` — permanent, the `absent:` idiom — so
    the 3,669 `*-updown-*` rows in the live revisit queue stop coming back
    every day for an answer that cannot change. That permanence is why
    `enabled` is read here too (minor, fix pass 3): with the non-sports half
    switched off nothing roots these rows anyway, and a PERMANENT marker is
    not the off switch's to write — `unroot_mechanical` already reads it."""
    if not cfg["whale"]["nonsports"]["enabled"]:
        return set()
    return {cond for cond in unrooted
            if whale_nonsports._excluded(
                cfg, (found[cond].get("events") or [{}])[0] or {}, found[cond])}


def event_roots(cfg, bucket, get, found, want_conds, res) -> dict:
    """{condition_id: (root, market_type, start_utc)} for the rows of ONE
    chunk that no sport slug can root (amendment 2026-09-05, Opus review C2).

    Until this existed, `whale_map._gamma_row` rooted a market from its slug
    prefix alone, so every HISTORICAL politics or crypto market a whale had
    traded stayed `sport=NULL`, `whale_score.positions_from_prints` dropped it
    as unmapped, and no politics whale could ever be scored — while the by-tag
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
    comes back through REVISIT_SQL on the next day's queue. A `no_root` row is
    a different matter — the caller keeps it out of `want_conds`, so it is
    never coming back and spends no call here either.
    """
    ns = cfg["whale"]["nonsports"]
    if not ns["enabled"] or res["root_calls"] >= int(ns["root_calls_per_cycle"]):
        return {}
    want: dict = {}
    for cond in want_conds:
        event_id = ((found[cond].get("events") or [{}])[0] or {}).get("id")
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


# Rows the exclusion has to reach BACKWARDS to. A pattern registered today
# cannot only govern what is rooted tomorrow: `markets` rows rooted before it
# existed keep their root, keep being scored by
# `whale_score.positions_from_prints` and keep being priced by the lane.
# Measured read-only on the live tape 2026-09-05: 11 already-rooted rows match
# (10 `crypto`, 1 `politics` — a solana strike ladder rooted politics off a
# multi-tagged event) out of 182 non-sports rows in all.
#
# The pass is self-terminating rather than budgeted, so it needs no config key
# of its own: it selects only rows that still carry a non-sports root, and
# clearing that root is what removes a row from its own result set forever.
# The `markets` UPDATE trips the mat triggers, so every wallet holding one of
# these positions re-materializes on its own — the point of doing it as an
# UPDATE rather than a delete.
UNROOT_SQL = """
    SELECT condition_id, slug, event_key FROM markets
     WHERE sport IN ({roots}) AND sport IS NOT NULL"""


def unroot_mechanical(cfg, con, now) -> int:
    """Clear the root of every already-rooted MECHANICAL PRICE SERIES row and
    mark it `no-root:` permanently. Tape only, no Gamma call, no ledger.

    A by-tag builder row carries no `slug` — Gamma's tag answer never fills
    one — but it does carry the finished `pm:<event_slug>` in `event_key`,
    which is the same event slug `_excluded` reads on the builder path. Both
    are offered here, so a row is judged by whichever its writer stored."""
    ns = cfg["whale"]["nonsports"]
    roots = sorted(set(ns["tags"].values()))
    if not ns["enabled"] or not roots:
        return 0
    doomed = []
    for r in con.execute(UNROOT_SQL.format(roots=",".join("?" * len(roots))), roots):
        key = r["event_key"] or ""
        event = {"slug": key[3:] if key.startswith("pm:") else ""}
        if whale_nonsports._excluded(cfg, event, {"slug": r["slug"]}):
            doomed.append(r["condition_id"])
    for cond in doomed:
        con.execute("UPDATE markets SET sport=NULL, market_type=NULL, start_utc=NULL,"
                    " last_seen=? WHERE condition_id=?", (now.isoformat(), cond))
        _set_meta(con, f"no-root:{cond}", now.strftime("%Y-%m-%d"))
    if doomed:
        con.commit()
        log.info("whale un-rooted %d mechanical price-series market(s)", len(doomed))
    return len(doomed)
