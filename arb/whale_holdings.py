"""L14 authoritative holdings check (design §9, L14 amendment 2026-09-04).

The print tape says what a whale BOUGHT; `data-api /positions?user=<w>` says
what it still HOLDS. Those are different facts, and the difference is the
whole point: a whale that exits through a RESTING order leaves no new BUY
print to age out and — before `taker_only: false` — no row of its own at all,
so the prints-based `left` test could watch a position we copied be sold out
from under us and see nothing move. This module is the second reading.

Rule 1: public, unauthenticated GETs only, paced by the collector's own shared
        token bucket (one bucket per process, whale.max_calls_per_min).
Rule 3: `positions?user=<wallet>&limit=N&offset=M` and the eight fields read
        below (conditionId, asset, outcomeIndex, size, avgPrice, totalBought,
        redeemable, currentValue) are the live-probed rows dated 2026-09-04 in
        data/verified_facts.md — including the offset-paging row that lets a
        full page keep going rather than being read as the whole wallet.
Rule 4: the cadence, the per-cycle cap, the page size, the page cap, the
        staleness bound and what is kept all come from `whale.exit`.
Rule 5: size, avg_price, total_bought and the summary's total_value_usd are
        6-place Decimal strings.
Rule 7: every poll is persisted, whether or not it moves an exit — the
        positions in the CALLER's tracked set as `holdings` rows, and the page
        as a whole as one `holdings_snapshots` row (retention addendum
        2026-09-04: storing every row of every page was ~374k rows/day.
        2026-09-05: the addendum's own `markets`-table tracked set had grown
        to every market backfill ever mapped, ~1.4M rows/day; `holdings_keep:
        universe` fixes it by tracking the lane's OWN current-cycle set).
Rule 9: writes go to the whale tape only — flat_bets is unreachable from here.
"""
from __future__ import annotations

import json
from decimal import Decimal

from arb.logutil import get_logger
from arb.models import q6
from arb.venues.base import get_json
from arb.whale_merge import ts
from arb.whale_rows import BAD_ROW
from arb.whale_tape import DATA_API, _bucket, _call, _meta, _set_meta

log = get_logger("arb.whale", "logs/whale.log")

# holdings columns/DDL: arb/whale_tape.py SCHEMA's `holdings` table.
COLS = ("observed_at, proxy_wallet, condition_id, asset, outcome_index, size,"
        " avg_price, total_bought, redeemable, raw_json")
# ... and the per-poll summary of the WHOLE page (`holdings_snapshots`).
SNAP_COLS = "observed_at, proxy_wallet, n_positions, total_value_usd, partial"
# The poll ATTEMPT, per wallet. A wallet that holds nothing returns an empty
# list and writes no `holdings` row, so the rows alone cannot say whether it
# was read — and "read, holds nothing" is precisely the reading that exits.
POLLED = "holdings_at:"
# Set to the snapshot's `observed_at` when that wallet was STILL full at
# holdings_max_pages (truncated, not fully read); "" otherwise. Meta, not a
# `holdings` column (2026-09-04 holdings-paging fix, smaller-change pick) —
# no migration needed for an existing tape file, whose `holdings` table
# CREATE TABLE IF NOT EXISTS would not gain a new column anyway.
PARTIAL = "holdings_partial:"
# Truncation WARNING, once per (wallet, UTC day) — meta, not a module-level
# set, so it survives a process restart and still fires again on a new day
# (2026-09-04 holdings-paging fix, item 3).
TRUNC_WARNED = "holdings_trunc_warned:"


def _row(r: dict, wallet: str, observed_at: str) -> tuple:
    idx = r.get("outcomeIndex")
    return (observed_at, wallet, str(r.get("conditionId") or ""),
            str(r.get("asset") or ""), int(idx) if idx is not None else None,
            str(q6(Decimal(str(r.get("size") or 0)))),
            str(q6(Decimal(str(r.get("avgPrice") or 0)))),
            str(q6(Decimal(str(r.get("totalBought") or 0)))),
            int(bool(r.get("redeemable"))), json.dumps(r, sort_keys=True))


def wallets_of(ctxs) -> list[str]:
    """The distinct wallets whose verdict can still move a position we hold:
    the signal whales on the BET'S OWN side, from the stored bet contexts. The
    other side's whales are not what `left` measures, and the per-cycle budget
    is small enough that spending it on them would starve ours."""
    out: set[str] = set()
    for ctx in ctxs:
        if not ctx:
            continue
        side = int(ctx["side"])
        out |= {x["wallet"] for x in ctx["whales"]
                if int(x.get("side", side)) == side}
    return sorted(out)


def due(cfg, con, wallets, now) -> list[str]:
    """At most `holdings_calls_per_cycle` wallets, stalest first, and only
    those whose last poll is at least `holdings_poll_secs` old (never polled
    sorts first). A roster inside the cadence x cap product is fully refreshed
    every `holdings_poll_secs`; a bigger one still rotates rather than
    starving, and `for_bet` refuses whatever has gone stale meanwhile."""
    ex = cfg["whale"]["exit"]
    cadence, cap = int(ex["holdings_poll_secs"]), int(ex["holdings_calls_per_cycle"])
    seen = {w: (_meta(con, POLLED + w) or "") for w in set(wallets)}
    overdue = [w for w, at in seen.items() if not at or ts(now) - ts(at) >= cadence]
    return sorted(overdue, key=lambda w: (seen[w], w))[:cap]


def _warn_truncated_once(con, wallet, observed_at, n) -> None:
    key = TRUNC_WARNED + wallet + ":" + observed_at[:10]     # UTC day
    if _meta(con, key):
        return
    _set_meta(con, key, "1")
    log.warning("whale holdings: %s still full after holdings_max_pages "
                "(%d positions read) — snapshot marked partial, a row-level "
                "absence in it is no reading, not a verified zero",
                wallet, n)


def _tracked(con, cfg, tracked) -> set[str] | None:
    """Which condition_ids a poll keeps rows for (retention fix 2026-09-05).
    `all` keeps the whole page (`None`). `universe` trusts the CALLER's own
    set — `poll_holdings`'s `tracked` argument, which the lane assembles as
    this cycle's live universe union every open bet's market (arb/lanes/
    whale.py) — never the `markets` table: that table is every market
    backfill has EVER mapped (14k+ rows), so under the pre-fix `markets` mode
    a signal whale's whole HISTORICAL portfolio still qualified as "tracked"
    (measured live 2026-09-05: +57,844 holdings rows in one hour, ~1.4M
    rows/day, ~400 MB/day — the tape's fastest-growing table). `markets` is
    that pre-fix behaviour, kept for rollback and for a caller that passes no
    explicit set: one read of the `markets` table per cycle."""
    keep = str(cfg["whale"]["exit"]["holdings_keep"])
    if keep == "all":
        return None
    if keep == "universe" and tracked is not None:
        return set(tracked)
    return {r[0] for r in con.execute("SELECT condition_id FROM markets")}


def _persist(con, chunk, wallet, observed_at, res, tracked) -> tuple[int, Decimal]:
    """One page: the rows worth keeping, written AND COMMITTED now — not
    batched with later pages, so a later page's failure or cap-cut can never
    discard what already landed (rule 7; 2026-09-04 holdings-paging fix item 2).
    Fixer-D review of a90a08d (C1): the commit used to be missing here and
    relied on a LATER `_set_meta` call in the same wallet's poll to flush this
    transaction — a page-2 failure `continue`s past that call, so a process
    death before some unrelated later write happened to commit left this page's
    real, already-fetched rows unreadable by any other connection,
    contradicting this docstring. Returns `(rows stored, total currentValue of
    the WHOLE page)`; the caller turns the page totals into the one
    `holdings_snapshots` row.

    Retention addendum 2026-09-04: a poll used to store the wallet's entire
    positions page — 500-2,000 rows a wallet every ~5 min, ~374k rows/day
    measured — when the fact the lane reads is the size of a signal whale's
    position in a market we hold or track. Rows outside `tracked` are dropped
    here, BEFORE `_row`'s json.dumps, and the page's shape survives in the
    summary row."""
    rows, value = [], Decimal(0)
    for r in chunk:
        try:
            value += Decimal(str(r.get("currentValue") or 0))
            if tracked is not None and str(r.get("conditionId") or "") not in tracked:
                continue
            rows.append(_row(r, wallet, observed_at))
        except BAD_ROW as e:
            res["bad_rows"] = res.get("bad_rows", 0) + 1
            log.warning("whale skipped an unreadable holdings row: %s", e)
    con.executemany(f"INSERT INTO holdings ({COLS}) VALUES ({','.join('?' * 10)})", rows)
    con.commit()
    return len(rows), value


def _persist_summary(con, wallet, observed_at, n, value, partial) -> None:
    """The whole-page fact, one row per poll — including a poll that found
    nothing, which is what lets `for_bet` tell "read, holds none of ours" (a
    real zero) from "read nothing at all" (no reading)."""
    con.execute(f"INSERT INTO holdings_snapshots ({SNAP_COLS}) VALUES (?,?,?,?,?)",
                (observed_at, wallet, int(n), str(q6(value)), int(bool(partial))))
    con.commit()


def poll_holdings(cfg, con, wallets, now, tracked=None, get=get_json) -> dict:
    """One cycle's holdings refresh. A wallet the venue refuses costs only
    itself: no watermark, no backoff (that guard belongs to the print feed,
    which is a different endpoint), and no `holdings_at` stamp, so it is
    retried next cycle rather than silently believed to hold what it did.

    `tracked`: the condition_ids worth PERSISTING, under `holdings_keep:
    universe` — the lane's own set (this cycle's live universe union every
    open bet's market). Ignored under `markets`/`all` (see `_tracked`); a
    caller that passes nothing falls back to the pre-2026-09-05 `markets`
    table read either way, so every existing caller is unaffected.

    A full page pages by `offset` (verified live 2026-09-04) until a short
    page or `holdings_max_pages`, each extra page spending one call of this
    cycle's own `holdings_calls_per_cycle` budget. That cap is checked before
    EVERY page, not just between wallets (2026-09-04 holdings-paging fix
    item 1: checking only between wallets let one admitted wallet run all
    `holdings_max_pages` regardless, cap - 1 + max_pages calls in the worst
    case). A wallet the cap cuts mid-way keeps whatever pages it already
    fetched (item 2: each page is persisted as it lands) but gets no
    `holdings_at` stamp — same as an unpolled wallet, so it sorts first in
    `due()` and the very next cycle retries it whole."""
    ex = cfg["whale"]["exit"]
    page, max_pages = int(ex["holdings_page_size"]), int(ex["holdings_max_pages"])
    cap = int(ex["holdings_calls_per_cycle"])
    res = {"wallets": 0, "rows": 0, "calls": 0, "errors": 0}
    bucket, observed_at = _bucket(cfg), now.isoformat()
    todo = due(cfg, con, wallets, now)
    tracked = _tracked(con, cfg, tracked) if todo else None
    for wallet in todo:
        if res["calls"] >= cap:
            break
        offset, n_seen, cut_by_cap, still_full = 0, 0, False, False
        value = Decimal(0)
        try:
            for _ in range(max_pages):
                if res["calls"] >= cap:
                    cut_by_cap = True            # this cycle's own budget, not the data
                    break
                chunk = _call(bucket, get, f"{DATA_API}/positions?user={wallet}"
                              f"&limit={page}&offset={offset}", cfg, res)
                n_seen += len(chunk)
                kept, page_value = _persist(con, chunk, wallet, observed_at, res, tracked)
                res["rows"] += kept
                value += page_value
                if len(chunk) < page:
                    break
                offset += page
            else:
                still_full = True                # still full after holdings_max_pages
        except Exception as e:  # noqa: BLE001 — one wallet, not the cycle
            res["errors"] += 1
            log.warning("whale holdings poll failed for %s: %s", wallet, e)
            continue                              # earlier pages this loop already
                                                   # persisted; no stamp, so still due
        if cut_by_cap:
            # C3 (fixer-D review of a90a08d): retried WHOLE from offset 0, not
            # resumed, so the pages it already fetched this cycle are re-persisted
            # again next cycle under a new observed_at — the old observed_at's rows
            # are now orphaned, and a wallet that keeps getting cut never stops
            # accumulating them. Unbounded today; the retention change
            # (.superpowers/sdd/retention-brief.md) is where `holdings` gets a
            # bound, and belongs there rather than a one-off cleanup here.
            continue                              # cut by the budget: same as unpolled,
                                                   # retried whole next cycle
        if still_full:
            _warn_truncated_once(con, wallet, observed_at, n_seen)
        _persist_summary(con, wallet, observed_at, n_seen, value, still_full)
        _set_meta(con, POLLED + wallet, observed_at)
        _set_meta(con, PARTIAL + wallet, observed_at if still_full else "")  # commits both
        res["wallets"] += 1
    return res


def _n_positions(con, wallet, at) -> int:
    """How many positions that snapshot's page actually held. Since the
    2026-09-04 addendum `holdings` keeps only the rows in markets we track, so
    zero rows no longer means an empty page — this is what still tells the two
    apart. A snapshot older than the addendum has no summary row and reads 0,
    which is exactly the pre-addendum behaviour for it."""
    row = con.execute("SELECT n_positions FROM holdings_snapshots "
                      "WHERE proxy_wallet=? AND observed_at=?", (wallet, at)).fetchone()
    return int(row[0] or 0) if row else 0


def for_bet(cfg, con, ctx, now) -> dict[str, Decimal]:
    """`{wallet: contracts of the bet's token it holds NOW}` for the signal
    whales on the bet's own side — what §9's second `left` test compares
    against the size they held at booking.

    A wallet is ABSENT when its last poll is older than `holdings_poll_secs` x
    `holdings_max_age_polls`, or never happened: no reading, so the caller
    falls back to the prints. Exiting a position on a stale holding is the
    same mistake as flipping it on a print that merely aged out.

    A wallet that IS fresh but whose snapshot no longer lists the token holds
    zero of it — a reading, not an absence, and the one this rule exists for.
    Since the 2026-09-04 retention addendum "no rows stored" no longer implies
    "held nothing": rows outside `markets` are dropped, so the page's own
    `n_positions` (`holdings_snapshots`) is what separates the two readings.
    An ENTIRELY EMPTY page is the one exception and is treated as no reading:
    rule 3 verified only that `positions?user=<w>` returns a live wallet's
    open positions, never what an empty body means. It could be a genuine
    total liquidation, but it is equally the shape a lookup that found nothing
    would take (a wallet-address form we hold differently, say), and that
    reading would exit EVERY bet behind that whale at once on one bad
    response. A whale that truly sold everything now prints its SELL leg
    anyway (`taker_only: false`, item B), so the prints-based test above still
    catches it — the lost case is small, the false-exit case is not.

    A wallet still full at `holdings_max_pages` (PARTIAL, 2026-09-04 holdings-
    paging fix) is the same shape of risk one page further out: a token
    missing from it may simply be sitting on the page that was never
    fetched, so it too is no reading. Presence is unaffected either way — a
    token this snapshot DID capture is trusted at its real size regardless.
    """
    ex = cfg["whale"]["exit"]
    max_age = int(ex["holdings_poll_secs"]) * int(ex["holdings_max_age_polls"])
    token, out = ctx.get("token"), {}
    if not token:
        return out
    for wallet in wallets_of([ctx]):
        at = _meta(con, POLLED + wallet)
        if not at or ts(now) - ts(at) > max_age:
            continue
        held = {r[0]: r[1] for r in con.execute(
            "SELECT asset, size FROM holdings WHERE proxy_wallet=? AND observed_at=?",
            (wallet, at))}
        if not held and not _n_positions(con, wallet, at):
            continue                                        # empty page: no reading
        if token in held:
            out[wallet] = Decimal(held[token])
        elif _meta(con, PARTIAL + wallet) != at:
            out[wallet] = Decimal(0)                         # fresh full read: a real zero
        # else: partial snapshot, token may be unread — no reading, fall back to prints
    return out
