"""Single source of truth for flat_bets lifecycle status (pass3#8).

The "latest lifecycle row per fingerprint" + "is this terminal?" logic was reimplemented in
tracker, settlement_health, ev_exit, and the CLV reader — and they had already DIVERGED on
whether EXITED counts as terminal, which is exactly how the EV double-settle hole crept in.
One definition here removes that whole class of bug.
"""
from __future__ import annotations

from typing import Any, Iterable

# A bet is "active" (still live, can transition) only in these two states.
ACTIVE_STATUSES = frozenset({"OPEN", "CONFIRMED"})
# Terminal = the bet is done; a terminal child means the parent is no longer open. EXITED
# (paper take-profit/stop) is terminal, mutually exclusive with SETTLED.
TERMINAL_STATUSES = frozenset({"CLOSED", "SETTLED", "EXITED"})
# The full set of rows that participate in deciding a fingerprint's lifecycle (annotation
# rows like VERIFIED/VANISHED/LIFETIME are NOT lifecycle transitions).
LIFECYCLE_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES


def latest_lifecycle_by_fingerprint(rows: Iterable[Any]) -> dict[str, Any]:
    """Given rows ordered by id ascending, the last lifecycle row seen per fingerprint."""
    latest: dict[str, Any] = {}
    for row in rows:
        if row["status"] in LIFECYCLE_STATUSES:
            latest[row["fingerprint"]] = row
    return latest


def active_rows_by_fingerprint(rows: Iterable[Any]) -> dict[str, Any]:
    """Fingerprints whose latest lifecycle row is still ACTIVE (OPEN/CONFIRMED)."""
    return {
        fingerprint: row
        for fingerprint, row in latest_lifecycle_by_fingerprint(rows).items()
        if row["status"] in ACTIVE_STATUSES
    }
