"""L14 the whale lane's market universe: which Polymarket sports markets the
collector tracks this cycle, and the tape's `markets` table.

Split out of arb/whale_tape.py on size alone (modules stay under ~300 lines);
same rules — the ledger is opened READ-ONLY here (rule 9: this file can only
write the tape), no network, no clock (`now` is always an argument).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

# `slug` is kept so a market Gamma answered for but whose league code is not
# in whale.slug_sport_prefixes can be re-mapped LOCALLY when that map grows —
# without it, an unmappable market would be stuck at sport=NULL forever.
MARKET_COLS = ("condition_id, sport, market_type, line, start_utc, event_key,"
               " tokens_json, slug, first_seen, last_seen")
# The ledger's PM sports quotes for ONE poll: served by idx_quotes_poll. A
# fetched_at range instead would scan the 37 GB table.
UNIVERSE_SQL = (
    "SELECT venue_market_id, sport, market_type, line, outcome, start_utc, event_key "
    "FROM quotes WHERE poll_id = (SELECT MAX(id) FROM polls WHERE kind='watch') "
    "AND venue='polymarket' AND category='sports'")


@dataclass(frozen=True)
class Market:
    condition_id: str
    sport: str | None
    market_type: str | None
    line: str | None
    start_utc: str | None
    event_key: str | None
    tokens: tuple = ()          # ((token_id, outcome_label), ...)


def _iso(value) -> str | None:
    """Normalise a venue timestamp to the ledger's own ISO form. Gamma sends
    '2026-08-09 09:00:00+00', which SQLite's strftime cannot parse."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def universe(store_ro, cfg, now) -> list[Market]:
    """Polymarket sports markets from the LATEST watch poll whose start lies in
    [now - lookback_hours, now + lookahead_hours] (spec §5 step 1)."""
    w = cfg["whale"]
    lo = now - timedelta(hours=float(w["lookback_hours"]))
    hi = now + timedelta(hours=float(w["lookahead_hours"]))
    by_cond: dict[str, dict] = {}
    for r in store_ro.execute(UNIVERSE_SQL):
        vmid = r["venue_market_id"] or ""
        start = _iso(r["start_utc"])
        if ":" not in vmid or start is None or not (lo <= datetime.fromisoformat(start) <= hi):
            continue
        cond, token = vmid.split(":", 1)
        m = by_cond.setdefault(cond, {"sport": r["sport"], "market_type": r["market_type"],
                                      "line": r["line"], "start_utc": start,
                                      "event_key": r["event_key"], "tokens": {}})
        m["tokens"][token] = r["outcome"]
    return [Market(condition_id=c, sport=m["sport"], market_type=m["market_type"],
                   line=m["line"], start_utc=m["start_utc"], event_key=m["event_key"],
                   tokens=tuple(sorted(m["tokens"].items()))) for c, m in by_cond.items()]


def persist_markets(con, markets, now):
    con.executemany(
        f"INSERT INTO markets ({MARKET_COLS}) VALUES ({','.join('?' * 10)}) "
        "ON CONFLICT(condition_id) DO UPDATE SET last_seen=excluded.last_seen, "
        "sport=COALESCE(excluded.sport, sport), "
        "market_type=COALESCE(excluded.market_type, market_type), "
        "start_utc=COALESCE(excluded.start_utc, start_utc), "
        "tokens_json=excluded.tokens_json",
        [(m.condition_id, m.sport, m.market_type, m.line, m.start_utc, m.event_key,
          json.dumps([list(t) for t in m.tokens]), None, now.isoformat(), now.isoformat())
         for m in markets])
    con.commit()
