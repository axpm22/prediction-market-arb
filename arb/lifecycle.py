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


def active_rows_via_index(con, kinds: tuple[str, ...] | None = None) -> dict[str, Any]:
    """S1: the SAME active set as `active_rows_by_fingerprint(all rows)`, but computed in the
    DB through the fingerprint/status indexes instead of scanning every `flat_bets` row in
    Python each poll (the old `SELECT * FROM flat_bets` was O(bets×polls)).

    For each fingerprint, the latest lifecycle row is the max-id row whose status is in
    LIFECYCLE_STATUSES; the fingerprint is active iff that row's status is ACTIVE. The query
    returns exactly those latest-and-active rows. When `kinds` is given the lifecycle is
    derived within that kind set (mirrors settlement_health's pre-filter, e.g. EV/ARB/CLOSE).
    """
    active_list = sorted(ACTIVE_STATUSES)
    life_list = sorted(LIFECYCLE_STATUSES)
    active_ph = ",".join("?" * len(active_list))
    life_ph = ",".join("?" * len(life_list))
    kind_outer = kind_inner = ""
    kind_params: list[str] = []
    if kinds:
        kind_ph = ",".join("?" * len(kinds))
        kind_outer = f" AND b.kind IN ({kind_ph})"
        kind_inner = f" AND c.kind IN ({kind_ph})"
        kind_params = list(kinds)
    sql = (
        f"SELECT b.* FROM flat_bets b "
        f"WHERE b.status IN ({active_ph}){kind_outer} "
        f"  AND b.id = (SELECT MAX(c.id) FROM flat_bets c "
        f"              WHERE c.fingerprint = b.fingerprint "
        f"                AND c.status IN ({life_ph}){kind_inner}) "
        f"ORDER BY b.id"
    )
    params = [*active_list, *kind_params, *life_list, *kind_params]
    return {row["fingerprint"]: row for row in con.execute(sql, params)}
