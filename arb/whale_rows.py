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


def _print_row(r: dict, ts: int, observed_at: str, source: str, full=True) -> tuple:
    """`/trades` and `/activity` rows share their field names (both verified
    2026-09-03). notional = price x size per spec §5; activity's own `usdcSize`
    disagrees with that product (it prices at the fill, not the shown price),
    so it is kept only inside raw_json. `ts` is already parsed by the caller —
    every row needs it for the page bound, kept or not (see _keep).

    `full=False` is the COMPACT row of the 2026-09-04 retention amendment:
    `title` and `raw_json` NULL, every other column as usual. raw_json is 864 B
    of the measured ~1.9 KB a print costs on disk, and a print in a market no
    universe covers is never read for anything but its notional and its wallet."""
    price = Decimal(str(r.get("price") or 0))
    size = Decimal(str(r.get("size") or 0))
    idx = r.get("outcomeIndex")
    return (observed_at, ts, str(r.get("transactionHash") or ""),
            str(r.get("proxyWallet") or ""), r.get("pseudonym"),
            str(r.get("conditionId") or ""), str(r.get("asset") or ""), r.get("outcome"),
            int(idx) if idx is not None else None, str(r.get("side") or ""),
            str(q6(price)), str(q6(size)), str(q6(price * size)),
            r.get("eventSlug"), r.get("slug"), r.get("title") if full else None, source,
            json.dumps(r, sort_keys=True) if full else None)


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


def _keep(con, body, ids, now, res, source="tape", cfg=None) -> list[int]:
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
    (the common case) pays only for one multiplication each."""
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
                                   full=raw_mode == "all" or (inside and raw_mode != "none")))
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
