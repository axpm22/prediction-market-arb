"""L14 authoritative holdings check (design §9, L14 amendment 2026-09-04).

The print tape says what a whale BOUGHT; `data-api /positions?user=<w>` says
what it still HOLDS. Those are different facts, and the difference is the
whole point: a whale that exits through a RESTING order leaves no new BUY
print to age out and — before `taker_only: false` — no row of its own at all,
so the prints-based `left` test could watch a position we copied be sold out
from under us and see nothing move. This module is the second reading.

Rule 1: public, unauthenticated GETs only, paced by the collector's own shared
        token bucket (one bucket per process, whale.max_calls_per_min).
Rule 3: `positions?user=<wallet>&limit=N` and the seven fields read below are
        the live-probed row dated 2026-09-04 in data/verified_facts.md. Paging
        past one page was NOT probed, so it is not attempted: a wallet at the
        page limit is logged once and what came back is used.
Rule 4: the cadence, the per-cycle cap, the page size and the staleness bound
        all come from `whale.exit`.
Rule 5: size, avg_price and total_bought are 6-place Decimal strings.
Rule 7: every snapshot is persisted, whether or not it moves an exit.
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
# The poll ATTEMPT, per wallet. A wallet that holds nothing returns an empty
# list and writes no `holdings` row, so the rows alone cannot say whether it
# was read — and "read, holds nothing" is precisely the reading that exits.
POLLED = "holdings_at:"
_WARNED: set[str] = set()


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


def poll_holdings(cfg, con, wallets, now, get=get_json) -> dict:
    """One cycle's holdings refresh. A wallet the venue refuses costs only
    itself: no watermark, no backoff (that guard belongs to the print feed,
    which is a different endpoint), and no `holdings_at` stamp, so it is
    retried next cycle rather than silently believed to hold what it did."""
    page = int(cfg["whale"]["exit"]["holdings_page_size"])
    res = {"wallets": 0, "rows": 0, "calls": 0, "errors": 0}
    bucket, observed_at = _bucket(cfg), now.isoformat()
    for wallet in due(cfg, con, wallets, now):
        try:
            body = _call(bucket, get, f"{DATA_API}/positions?user={wallet}"
                         f"&limit={page}", cfg, res)
        except Exception as e:  # noqa: BLE001 — one wallet, not the cycle
            res["errors"] += 1
            log.warning("whale holdings poll failed for %s: %s", wallet, e)
            continue
        if len(body) >= page and wallet not in _WARNED:
            _WARNED.add(wallet)
            log.warning("whale holdings: %s returned a full page of %d positions "
                        "— paging past one page is unverified (rule 3), so this "
                        "snapshot is what came back, not the whole wallet",
                        wallet, len(body))
        rows = []
        for r in body:
            try:
                rows.append(_row(r, wallet, observed_at))
            except BAD_ROW as e:
                res["bad_rows"] = res.get("bad_rows", 0) + 1
                log.warning("whale skipped an unreadable holdings row: %s", e)
        con.executemany(f"INSERT INTO holdings ({COLS}) VALUES ({','.join('?' * 10)})",
                        rows)
        _set_meta(con, POLLED + wallet, observed_at)        # commits both
        res["wallets"] += 1
        res["rows"] += len(rows)
    return res


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
    An ENTIRELY EMPTY page is the one exception and is treated as no reading:
    rule 3 verified only that `positions?user=<w>` returns a live wallet's
    open positions, never what an empty body means. It could be a genuine
    total liquidation, but it is equally the shape a lookup that found nothing
    would take (a wallet-address form we hold differently, say), and that
    reading would exit EVERY bet behind that whale at once on one bad
    response. A whale that truly sold everything now prints its SELL leg
    anyway (`taker_only: false`, item B), so the prints-based test above still
    catches it — the lost case is small, the false-exit case is not.
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
        if held:
            out[wallet] = Decimal(held.get(token, 0))
    return out
