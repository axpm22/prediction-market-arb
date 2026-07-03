"""`arb check-invariants` — ledger integrity scan run at the end of every settle cycle.

Five checks from FIX_SETTLED_REBOOK_LOOP (unchanged). Observability only: violations log
CRITICAL, never block settlement (a check that could halt the ledger would be a new failure
mode). Checks 1–3 reuse the one-shot `loop_audit.detect_loop_rows` detector so the runtime
guard and the historical audit can never diverge; 4–5 are added here.

  1. no fingerprint has > 1 settled row;
  2. no row has placed_at >= event start_utc (where start is known);
  3. no row settled faster than the poll interval WITHOUT a settle-tripwire/instant-settle
     annotation (the tripwire's own catches are excused — they are already handled);
  4. no reappearance chain longer than tracker.max_reappearances in any 24h window;
  5. every settled row's P&L is Decimal-clean (q6, no float drift).

Winner-loops and loser-loops are treated identically (integrity, not P&L sign).
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .pricing import q6
from .store import Store
from .experimental.analysis.loop_audit import detect_loop_rows

_CHECK_NUM = {"duplicate-settlement": 1, "post-start": 2, "instant-settle": 3}
# A row the tripwire already caught (settle-tripwire) or a legitimately-annotated instant-settle
# is not a new violation for check 3 — it is the guard working.
_EXCUSED_TAGS = ("settle-tripwire", "instant-settle")

_PNL_COLS = ("pnl_optimistic", "pnl_confirmed", "pnl_kelly_optimistic", "pnl_kelly_confirmed")


def _parse_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _excused_ids(con: sqlite3.Connection) -> set[int]:
    try:
        placeholders = ",".join("?" * len(_EXCUSED_TAGS))
        rows = con.execute(
            f"SELECT DISTINCT bet_id FROM annotations WHERE tag IN ({placeholders})", _EXCUSED_TAGS)
        return {int(r["bet_id"]) for r in rows}
    except sqlite3.OperationalError:
        return set()  # annotations table absent → nothing excused yet


def _reappearance_violations(con: sqlite3.Connection, max_reappearances: int) -> list[str]:
    times: dict[str, list[datetime]] = defaultdict(list)
    for row in con.execute("SELECT fingerprint, placed_at FROM flat_bets WHERE placed_at IS NOT NULL"):
        t = _parse_utc(row["placed_at"])
        if t is not None:
            times[str(row["fingerprint"])].append(t)
    window = timedelta(hours=24)
    out: list[str] = []
    for fp, ts in times.items():
        ts.sort()
        j = 0
        peak = 0
        for i in range(len(ts)):
            while ts[i] - ts[j] > window:
                j += 1
            peak = max(peak, i - j + 1)
        if peak > max_reappearances:
            out.append(f"[4] reappearance-chain: fingerprint {fp[:14]} has {peak} rows in a 24h "
                       f"window (> tracker.max_reappearances={max_reappearances})")
    return out


def _decimal_violations(con: sqlite3.Connection) -> list[str]:
    out: list[str] = []
    for row in con.execute(f"SELECT id, {', '.join(_PNL_COLS)} FROM flat_bets WHERE status='SETTLED'"):
        for col in _PNL_COLS:
            val = row[col]
            if val in (None, ""):
                continue
            try:
                d = Decimal(str(val))
            except InvalidOperation:
                out.append(f"[5] bet {row['id']} {col} not Decimal-parseable: {val!r}")
                continue
            if d != q6(d):
                out.append(f"[5] bet {row['id']} {col} not q6-clean (float drift): {val}")
    return out


def check_invariants(store: Store, poll_interval_secs: int, max_reappearances: int) -> list[str]:
    """Return a list of violation strings (empty == silent/OK). Read-only."""
    with store.connect() as con:
        excused = _excused_ids(con)
        violations: list[str] = []
        for f in detect_loop_rows(con, float(poll_interval_secs)):
            checks = list(f["checks"])
            if f["id"] in excused and "instant-settle" in checks:
                checks.remove("instant-settle")
            for c in checks:
                violations.append(
                    f"[{_CHECK_NUM[c]}] {c}: bet {f['id']} fp={str(f['fingerprint'])[:14]} "
                    f"placed={str(f['placed_at'])[:19]} settled={str(f['settled_at'])[:19]} "
                    f"result={f['result']} pnl={f['pnl_optimistic']}")
        violations += _reappearance_violations(con, max_reappearances)
        violations += _decimal_violations(con)
    return violations


def format_invariants(violations: list[str]) -> str:
    if not violations:
        return "check-invariants: OK — no ledger invariant violations."
    head = (f"check-invariants: {len(violations)} violation(s) "
            f"(CRITICAL; observability only — settlement not blocked):")
    return "\n".join([head, *(f"  {v}" for v in violations)])
