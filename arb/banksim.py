"""Bankroll-constrained paper book: what a fixed pot would actually have caught.

The bench measures edge per dollar. This measures the thing that turned out to
bind harder — whether you had cash free when the arb appeared. A locked pair
ties its stake up until the event settles (~2.8 days measured), while new pairs
arrive ~2.8/day, so ~8 sit open at once and a small pot spends most of its life
fully deployed. Missing an arb for lack of capital is not a detection failure
and must not be reported as one.

Read-only over the ledger (rule 9 is not merely respected, it is unreachable:
nothing here issues INSERT/UPDATE against any arb table). Results go to their
own JSON file.

Three numbers per bankroll, and the third is the one people forget:
  taken       arbs the pot could fund
  missed      arbs it could not — capital, not quality
  utilisation share of the window with capital deployed; low utilisation means
              the pot is too big for the opportunity flow, not too small
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.models import LOCKED_KINDS, q6

DEFAULT_BANKROLLS = (250, 500, 1000)


@dataclass
class Position:
    key: str
    entry: datetime
    exit: datetime          # entry -> event end + venue settlement lag
    edge: Decimal
    cap_usd: Decimal        # most this position could absorb
    cap_imputed: bool       # True when depth was not recorded on every leg
    confirmed: bool         # survived a re-quote (verify lane oracle)
    kind: str = ""
    venue_pair: str = ""


def _lockup(cfg, row) -> timedelta:
    """Entry to capital release. Deliberately NOT the ledger's settled event:
    locked constructs book LOCKED_AT_ENTRY immediately, which is a P&L
    convention, not when the cash comes back."""
    dur = cfg["sport_typical_duration_hours"]
    lag = cfg["venue_settlement_lag_hours"]
    hours = float(dur.get(row["sport"], dur.get("default", 3)))
    venues = [v for v in (row["venue_pair"] or "").split(":") if v in lag]
    hours += max([float(lag[v]) for v in venues] or [24.0])
    if row["event_start_utc"]:
        start = datetime.fromisoformat(row["event_start_utc"])
        entry = datetime.fromisoformat(row["created_at"])
        return max(start + timedelta(hours=hours) - entry, timedelta(hours=1))
    return timedelta(hours=hours)


def positions(cfg, store, start: str, end: str) -> list[Position]:
    """Deduped locked flags in [start, end), each with its capital lockup and
    the most it could absorb. Depth comes from the stored quote where every leg
    has it, and is imputed from the median otherwise — flagged either way,
    because an imputed cap is an assumption, not a measurement."""
    from arb.projection import projection_frame
    ks = ",".join("?" * len(LOCKED_KINDS))
    rows = store.query(
        f"SELECT * FROM flat_bets WHERE created_at >= ? AND created_at < ? "
        f"AND kind IN ({ks}) ORDER BY created_at", (start, end, *LOCKED_KINDS))
    frame = projection_frame(cfg, store, start[:10], end[:10])
    caps = {p["position_key"]: p["ladder_stake_usd"] for p in frame["positions"]}
    med = (sorted(caps.values())[len(caps) // 2] if caps else Decimal(50))

    out, seen = [], set()
    for r in rows:
        row = dict(r)
        if row["position_key"] in seen:
            continue
        seen.add(row["position_key"])
        edge = Decimal(str(row["edge_points"] or 0))
        if edge <= 0:
            continue
        entry = datetime.fromisoformat(row["created_at"])
        confirmed = bool(store.query(
            "SELECT 1 FROM bet_events WHERE bet_id=? AND kind='confirmed' LIMIT 1",
            (row["id"],)))
        out.append(Position(
            key=row["position_key"], entry=entry, exit=entry + _lockup(cfg, row),
            edge=edge, cap_usd=caps.get(row["position_key"], med),
            cap_imputed=row["position_key"] not in caps, confirmed=confirmed,
            kind=row["kind"], venue_pair=row["venue_pair"] or ""))
    return out


def simulate(pos: list[Position], bankroll: Decimal, per_frac: Decimal,
             require_confirmed: bool = False, window_end=None) -> dict:
    """Walk arrivals in time order. Capital is released the moment a prior
    position's event settles, BEFORE the new arrival is sized — releasing after
    understates what the pot can do (it made a first cut of this look 4x worse
    than reality)."""
    free = Decimal(bankroll)
    open_: list[tuple[datetime, Decimal]] = []
    out = {"taken": 0, "missed_capital": 0, "skipped_unconfirmed": 0,
           "pnl": Decimal(0), "deployed": Decimal(0), "imputed_share": 0.0,
           "capital_seconds": Decimal(0), "peak_deployed": Decimal(0)}
    imputed = 0
    for p in sorted(pos, key=lambda x: x.entry):
        for exit_at, size in [o for o in open_ if o[0] <= p.entry]:
            free += size
        open_ = [o for o in open_ if o[0] > p.entry]
        if require_confirmed and not p.confirmed:
            out["skipped_unconfirmed"] += 1
            continue
        size = min(Decimal(bankroll) * per_frac, p.cap_usd, free)
        if size <= Decimal("0.01"):
            out["missed_capital"] += 1
            continue
        free -= size
        open_.append((p.exit, size))
        out["taken"] += 1
        out["deployed"] += size
        out["pnl"] += size * p.edge
        # clamp to the window: a position entered near the end stays locked for
        # days AFTER it, and counting that time made utilisation exceed 100%
        stop = min(p.exit, window_end) if window_end else p.exit
        out["capital_seconds"] += size * Decimal(max((stop - p.entry).total_seconds(), 0))
        out["peak_deployed"] = max(out["peak_deployed"], Decimal(bankroll) - free)
        imputed += p.cap_imputed
    out["pnl"] = q6(out["pnl"])
    out["deployed"] = q6(out["deployed"])
    out["imputed_share"] = round(imputed / out["taken"], 3) if out["taken"] else 0.0
    return out


def run(cfg, store, start: datetime, end: datetime,
        bankrolls=DEFAULT_BANKROLLS, per_frac=Decimal("0.25")) -> dict:
    pos = positions(cfg, store, start.isoformat(), end.isoformat())
    window_secs = Decimal(max((end - start).total_seconds(), 1))
    res = {"start": start.isoformat(), "end": end.isoformat(),
           "n_positions": len(pos),
           "n_confirmed": sum(1 for p in pos if p.confirmed),
           "arrivals_per_day": round(len(pos) / (float(window_secs) / 86400), 2)
           if window_secs else 0,
           "bankrolls": {}}
    for b in bankrolls:
        r = simulate(pos, Decimal(b), per_frac, window_end=end)
        r["utilisation"] = round(float(r["capital_seconds"] / (Decimal(b) * window_secs)), 3)
        r["pnl_per_day"] = float(q6(r["pnl"] / (window_secs / 86400)))
        # what the same pot earns if it only funds flags that survived a re-quote
        rc = simulate(pos, Decimal(b), per_frac, require_confirmed=True,
                      window_end=end)
        r["pnl_confirmed_only"] = float(rc["pnl"])
        r["taken_confirmed_only"] = rc["taken"]
        del r["capital_seconds"]
        r["pnl"] = float(r["pnl"])
        r["deployed"] = float(r["deployed"])
        r["peak_deployed"] = float(r["peak_deployed"])
        res["bankrolls"][str(b)] = r
    return res


def append_snapshot(path: str, snap: dict) -> None:
    """Append-only JSONL — the run's own audit trail, never the ledger."""
    with open(path, "a") as fh:
        fh.write(json.dumps(snap, default=str) + "\n")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
