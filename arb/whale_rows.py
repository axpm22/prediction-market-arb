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

PRINT_COLS = ("observed_at, ts, tx_hash, proxy_wallet, pseudonym, condition_id, asset,"
              " outcome, outcome_index, side, price, size, notional_usd, event_slug,"
              " slug, title, source, raw_json")
# A row the venue sent that we cannot read: bad number, missing timestamp. Not
# an outage — the page around it is fine, so it is skipped and counted.
BAD_ROW = (KeyError, ValueError, TypeError, InvalidOperation)


def _print_row(r: dict, observed_at: str, source: str) -> tuple:
    """`/trades` and `/activity` rows share their field names (both verified
    2026-09-03). notional = price x size per spec §5; activity's own `usdcSize`
    disagrees with that product (it prices at the fill, not the shown price),
    so it is kept only inside raw_json. `timestamp` is read STRICTLY: a row
    without one cannot be ordered, and a 0 would drag the watermark back."""
    price = Decimal(str(r.get("price") or 0))
    size = Decimal(str(r.get("size") or 0))
    idx = r.get("outcomeIndex")
    return (observed_at, int(r["timestamp"]), str(r.get("transactionHash") or ""),
            str(r.get("proxyWallet") or ""), r.get("pseudonym"),
            str(r.get("conditionId") or ""), str(r.get("asset") or ""), r.get("outcome"),
            int(idx) if idx is not None else None, str(r.get("side") or ""),
            str(q6(price)), str(q6(size)), str(q6(price * size)),
            r.get("eventSlug"), r.get("slug"), r.get("title"), source,
            json.dumps(r, sort_keys=True))


def _keep(con, body, ids, now, res, source="tape") -> list[int]:
    """Persist the rows we track; duplicates are rejected by the unique index.
    Returns the timestamps of every row that PARSED — the caller's paging
    bounds come from those, never from the raw body, because one unreadable
    row must not abort the page (which would also write a backoff the venue
    never earned)."""
    observed_at = now.isoformat()
    rows, stamps = [], []
    for r in body:
        try:
            row = _print_row(r, observed_at, source)
        except BAD_ROW as e:
            res["bad_rows"] = res.get("bad_rows", 0) + 1
            log.warning("whale skipped an unreadable print row: %s", e)
            continue
        stamps.append(row[1])
        if ids is None or row[5] in ids:
            rows.append(row)
    before = con.total_changes
    con.executemany(f"INSERT OR IGNORE INTO prints ({PRINT_COLS}) "
                    f"VALUES ({','.join('?' * 18)})", rows)
    con.commit()
    kept = con.total_changes - before
    res["rows"] = res.get("rows", 0) + kept
    res["dupes"] = res.get("dupes", 0) + len(rows) - kept
    return stamps
