"""Offline policy replay (PROMPT_REPLAY): re-run detect() over quotes already
persisted by SPEC rule 7, so a change to thresholds or detection is testable
against weeks of real data with zero API credits and zero live HTTP.

Read-only, always: opens the DB via `file:...?mode=ro`, streams one poll at a
time (idx_quotes_poll), writes nothing anywhere. Groups come from the persisted
`event_key` (stamped by matching at scan time, arb/matching.py:182) —
build_groups() is NOT re-run, so replay has no live-MLB-schedule dependency.

Scope: the sports ARB/XSPREAD/DIVERGENCE detect path only. negRisk and political
lanes consume raw books/market dicts, not Quote rows, and are not replayed.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from arb.arbitrage import detect
from arb.matching import MatchedGroup, _suspect_reason
from arb.models import Event, Quote

NY = ZoneInfo("America/New_York")
KIND_MODE = {"watch": "pregame", "live": "live"}
MAX_EXAMPLES = 20  # display cap on diff examples; counts are always complete


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _dt(v) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def quote_from_row(row) -> Quote:
    """Inverse of arb.cycle._quote_row. home/away come from the event_key
    (`sport:date:away:home[:dhN]`) because quotes never store the venue-raw
    names; venue_side is reconcile-only and unread by detect()."""
    r = dict(row)
    parts = r["event_key"].split(":")
    away, home = (parts[2], parts[3]) if len(parts) >= 4 else ("", "")
    return Quote(
        event_key=r["event_key"], venue=r["venue"], book=r["book"],
        market_type=r["market_type"], line=r["line"], outcome_label=r["outcome"],
        side=r["side"], raw_price=_dec(r["raw_price"]), cost=_dec(r["cost"]),
        fee_cost=_dec(r["fee_cost"]), depth_usd=_dec(r["depth_usd"]),
        ladder_json=r["ladder_json"], live=bool(r["live"]), fetched_at=_dt(r["fetched_at"]),
        category=r["category"], sport=r["sport"], bid=_dec(r["bid"]),
        volume_24h=_dec(r["volume_24h"]), home=home, away=away,
        start_utc=_dt(r["start_utc"]), start_grade=r["start_grade"],
        venue_market_id=r["venue_market_id"] or "", venue_side="")


def groups_from_quotes(cfg, quotes: list[Quote]) -> list[MatchedGroup]:
    """One MatchedGroup per persisted event_key — exact by construction, since
    matching already did the grouping when these rows were written. `suspect` is
    recomputed with the live predicate: every input it reads (start_utc,
    market_type, line, outcome, side, cost — arb/matching.py:109) is persisted."""
    by_key: dict[str, list[Quote]] = {}
    for q in quotes:
        if q.event_key:
            by_key.setdefault(q.event_key, []).append(q)
    groups = []
    for key, qs in by_key.items():
        starts = [q.start_utc for q in qs if q.start_utc]
        parts = key.split(":")
        event = Event(event_key=key, category=qs[0].category, sport=qs[0].sport,
                      home=parts[3] if len(parts) >= 4 else "",
                      away=parts[2] if len(parts) >= 4 else "",
                      start_utc=min(starts) if starts else None,
                      start_grade=("verified" if any(q.start_grade == "verified" for q in qs)
                                   else "nominal" if starts else "absent"))
        groups.append(MatchedGroup(event=event, quotes=qs,
                                   suspect=_suspect_reason(cfg, qs)))
    return groups


@dataclass
class ReplaySummary:
    kind: str
    polls: int = 0
    quotes: int = 0
    opps: Counter = field(default_factory=Counter)      # (kind, status) -> n
    rejects: Counter = field(default_factory=Counter)   # reason -> n
    edge_total: Decimal = Decimal(0)
    days: Counter = field(default_factory=Counter)      # NY date -> polls replayed
    both: int = 0            # fingerprint flagged then AND now
    replay_only: int = 0     # current code/config would newly flag it
    stored_only: int = 0     # current code/config no longer flags it
    examples: list = field(default_factory=list)        # (set, fp, event_key, edge)


def open_ro(path) -> sqlite3.Connection:
    """Read-only URI handle on a DB the supervisor may be actively writing."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")  # same values as arb/store.py:29-32
    return con


def replay(cfg, con, from_date: str, to_date: str | None = None,
           kind: str = "watch") -> ReplaySummary:
    """Re-detect every poll of `kind` in [from_date, to_date] (America/New_York
    days, consistent with event_key dating), with the poll's own started_at as
    detect()'s clock — the only wall-clock detect reads (arb/arbitrage.py:84)."""
    mode = KIND_MODE[kind]
    lo = datetime.fromisoformat(from_date).replace(tzinfo=NY)
    hi = datetime.fromisoformat(to_date or from_date).replace(tzinfo=NY) + timedelta(days=1)
    s = ReplaySummary(kind=kind)
    # SQL prefilter is deliberately a day wide either side — started_at is a UTC
    # ISO string, so a string compare only approximates a NY day; Python decides.
    polls = con.execute(
        "SELECT id, started_at FROM polls WHERE kind = ? AND started_at >= ? "
        "AND started_at < ? ORDER BY id",
        (kind, (lo - timedelta(days=1)).astimezone(timezone.utc).isoformat(),
         (hi + timedelta(days=1)).astimezone(timezone.utc).isoformat())).fetchall()

    for poll in polls:
        started = _dt(poll["started_at"])
        if not (lo <= started.astimezone(NY) < hi):
            continue
        rows = con.execute("SELECT * FROM quotes WHERE poll_id = ?",
                           (poll["id"],)).fetchall()  # idx_quotes_poll
        quotes = [quote_from_row(r) for r in rows]
        opps, rejects = detect(cfg, groups_from_quotes(cfg, quotes),
                               now=started, mode=mode)
        s.polls += 1
        s.quotes += len(quotes)
        s.days[started.astimezone(NY).date().isoformat()] += 1
        for o in opps:
            s.opps[(o.kind, o.status)] += 1
            s.edge_total += o.edge
        for r in rejects:
            s.rejects[r["reason"]] += 1
        _diff_poll(con, poll["id"], opps, s)
    return s


def _diff_poll(con, poll_id: int, opps: list, s: ReplaySummary) -> None:
    """Replayed fingerprints vs the ones persisted for this poll. A difference
    means THE CODE OR CONFIG CHANGED — the stored history is immutable and is
    never the thing that moved."""
    stored = {r["fingerprint"]: r for r in con.execute(
        "SELECT fingerprint, event_key, edge FROM opportunities WHERE poll_id = ?",
        (poll_id,)).fetchall()}
    replayed = {o.fingerprint: o for o in opps}
    s.both += len(stored.keys() & replayed.keys())
    for fp in sorted(replayed.keys() - stored.keys()):
        s.replay_only += 1
        o = replayed[fp]
        if len(s.examples) < MAX_EXAMPLES:
            s.examples.append(("replay-only", fp, o.event.event_key, str(o.edge)))
    for fp in sorted(stored.keys() - replayed.keys()):
        s.stored_only += 1
        r = stored[fp]
        if len(s.examples) < MAX_EXAMPLES:
            s.examples.append(("stored-only", fp, r["event_key"], r["edge"]))
