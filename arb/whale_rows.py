"""One print row, parsed and persisted. Split out of arb/whale_tape.py on
size alone (modules stay under ~300 lines) — same rules, same file: writes go
only to whale.tape_db, money is Decimal at the store boundary (rule 5).
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from arb.logutil import get_logger
from arb.models import q6

log = get_logger("arb.whale", "logs/whale.log")

# prints columns/DDL: arb/whale_tape.py SCHEMA's `prints` table (kept in sync
# by hand).
PRINT_COLS = ("observed_at, ts, tx_hash, proxy_wallet, pseudonym, condition_id, asset,"
              " outcome, outcome_index, side, price, size, notional_usd, event_slug,"
              " slug, title, source, raw_json")
# A row the venue sent that we cannot read: bad number, missing timestamp. Not
# an outage — the page around it is fine, so it is skipped and counted.
BAD_ROW = (KeyError, ValueError, TypeError, InvalidOperation)


def _notional(r: dict) -> Decimal:
    """price x size, the one definition of a print's size (spec §5)."""
    return Decimal(str(r.get("price") or 0)) * Decimal(str(r.get("size") or 0))


def _print_row(r: dict, ts: int, observed_at: str, source: str, full=True,
               keep_raw=True) -> tuple:
    """`/trades` and `/activity` rows share their field names (both verified
    2026-09-03). notional = price x size per spec §5; activity's own `usdcSize`
    disagrees with that product (it prices at the fill, not the shown price),
    so it is kept only inside raw_json. `ts` is already parsed by the caller —
    every row needs it for the page bound, kept or not (see _keep).

    `full=False` is the COMPACT row of the 2026-09-04 retention amendment:
    `title` and `raw_json` NULL, every other column as usual. raw_json is 864 B
    of the measured ~1.9 KB a print costs on disk, and a print in a market no
    universe covers is never read for anything but its notional and its wallet.

    `keep_raw=False` (2026-09-05, backfill compact) NULLs raw_json alone, even
    when `full=True` keeps title — the backfill's compact row (see `_keep`'s
    `compact=` argument). `title` is an observed field kept as fetched (rule 7);
    `raw_json` is a convenience copy of columns already stored above, so
    dropping it loses nothing scoring or hygiene reads."""
    price = Decimal(str(r.get("price") or 0))
    size = Decimal(str(r.get("size") or 0))
    idx = r.get("outcomeIndex")
    return (observed_at, ts, str(r.get("transactionHash") or ""),
            str(r.get("proxyWallet") or ""), r.get("pseudonym"),
            str(r.get("conditionId") or ""), str(r.get("asset") or ""), r.get("outcome"),
            int(idx) if idx is not None else None, str(r.get("side") or ""),
            str(q6(price)), str(q6(size)), str(q6(price * size)),
            r.get("eventSlug"), r.get("slug"), r.get("title") if full else None, source,
            json.dumps(r, sort_keys=True) if full and keep_raw else None)


def doors(cfg) -> tuple[Decimal | None, str]:
    """`(global floor, raw_json mode)` — the retention amendment of 2026-09-04
    read out of config once per page, never per row. A floor of None is the
    pre-amendment tape: the universe door only. `cfg=None` (the per-wallet
    backfill, which fetches a KNOWN whale's whole history and whose sub-floor
    prints are exactly what hygiene reads) keeps everything, as it always has."""
    if cfg is None:
        return None, "all"
    r = cfg["whale"]["retention"]
    floor = Decimal(str(r["global_min_print_usd"])) if r["scope"] == "global" else None
    return floor, str(r["raw_json"])


def _keep(con, body, ids, now, res, source="tape", cfg=None, compact=False) -> list[int]:
    """Persist the rows we keep; duplicates are rejected by the unique index.
    Returns the timestamps of every row whose `timestamp` PARSED — the
    caller's paging bounds come from those, never from the raw body, because
    one unreadable row must not abort the page (which would also write a
    backoff the venue never earned).

    TWO doors since 2026-09-04 (proposal §2, registered in PREREGISTRATION.md):
    a row whose `conditionId` is in `ids` is kept in FULL — hygiene's `mm` test
    reads prints far below `min_print_usd` and would go blind without them —
    and a row outside every universe is kept only at or above the global floor,
    and compact. `conditionId in ids` is still checked before the Decimal/q6
    conversions and json.dumps below, so a page of rows outside our universe
    (the common case) pays only for one multiplication each.

    `compact=True` (2026-09-05, `whale.retention.backfill_compact`, the
    per-wallet backfill's own caller) NULLs raw_json regardless of `ids`/`cfg`
    — orthogonal to the two doors above, which govern row SELECTION and
    title, not this. It has no effect here on the doors themselves: the
    backfill caller always passes `ids=None`, which already keeps every row
    (`inside` is unconditionally true), as it always has."""
    observed_at = now.isoformat()
    floor, raw_mode = doors(cfg)
    rows, stamps = [], []
    for r in body:
        try:
            ts = int(r["timestamp"])
        except BAD_ROW as e:
            res["bad_rows"] = res.get("bad_rows", 0) + 1
            log.warning("whale skipped an unreadable print row: %s", e)
            continue
        stamps.append(ts)
        inside = ids is None or str(r.get("conditionId") or "") in ids
        if not inside:
            if floor is None:
                continue
            try:
                if _notional(r) < floor:
                    continue
            except BAD_ROW as e:
                res["bad_rows"] = res.get("bad_rows", 0) + 1
                log.warning("whale skipped an unreadable print row: %s", e)
                continue
        try:
            rows.append(_print_row(r, ts, observed_at, source,
                                   full=raw_mode == "all" or (inside and raw_mode != "none"),
                                   keep_raw=not compact))
        except BAD_ROW as e:
            res["bad_rows"] = res.get("bad_rows", 0) + 1
            log.warning("whale skipped an unreadable print row: %s", e)
            continue
    before = con.total_changes
    con.executemany(f"INSERT OR IGNORE INTO prints ({PRINT_COLS}) "
                    f"VALUES ({','.join('?' * 18)})", rows)
    con.commit()
    kept = con.total_changes - before
    res["rows"] = res.get("rows", 0) + kept
    res["dupes"] = res.get("dupes", 0) + len(rows) - kept
    return stamps


# Bounded-chunk size for compact_backfill_rows below — a loop-shape constant
# like LEADERBOARD_PAGE/GAMMA_IDS_PER_CALL in whale_tape.py, not a threshold
# (rule 4 covers business figures, not an UPDATE's own batch width).
_COMPACT_BATCH = 5000


def compact_backfill_rows(con, batch=_COMPACT_BATCH) -> tuple[int, int]:
    """`arb whale-compact --backfill`'s one-shot cleanup: NULL raw_json on
    every EXISTING `source='activity'` row that still carries one — rows
    backfilled before `whale.retention.backfill_compact` landed (2026-09-05).
    Only the tape's own `prints` table; nothing else on the tape is touched,
    and the ledger is never opened (this function never sees a Store).

    Bounded batches — SELECT a page of (id, byte length), NULL just those ids,
    commit, repeat — so a multi-million-row tape does not sit in one giant
    transaction. `LENGTH(raw_json)` is read in the SAME SELECT that names the
    ids, before they are nulled, so the byte count is exact, not an estimate.

    Returns (rows touched, bytes freed). Freed bytes are NOT reclaimed on
    disk by this — SQLite returns an UPDATE's freed page to its own internal
    freelist for reuse, not to the OS — only `VACUUM` (or `VACUUM INTO`, as
    `arb backup` already runs) shrinks the file, and that takes an exclusive
    lock this one-shot deliberately does not take. Run VACUUM offline,
    separately, when the operator decides the lock is worth it."""
    touched = freed = 0
    while True:
        page = con.execute(
            "SELECT id, LENGTH(raw_json) FROM prints WHERE source='activity' "
            "AND raw_json IS NOT NULL LIMIT ?", (batch,)).fetchall()
        if not page:
            break
        con.executemany("UPDATE prints SET raw_json=NULL WHERE id=?",
                        [(row[0],) for row in page])
        con.commit()
        touched += len(page)
        freed += sum(row[1] for row in page)
    return touched, freed
