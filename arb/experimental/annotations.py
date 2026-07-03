"""Immutable bet annotations — bets are never edited; context accumulates beside them.

Tags like bug:<issue>, settler-error, match-suspect let reports show headline P&L and
clean P&L (annotated bets excluded) side by side. A written reason is mandatory:
nothing in this ledger is ever excluded silently.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from arb.store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bet_id) REFERENCES flat_bets(id)
);
"""


def annotate(store: Store, bet_id: int, tag: str, reason: str) -> int:
    if not reason.strip():
        raise ValueError("Annotations require a written reason; nothing is excluded silently.")
    with store.connect() as con:
        con.executescript(SCHEMA)
        exists = con.execute("SELECT 1 FROM flat_bets WHERE id = ?", (bet_id,)).fetchone()
        if exists is None:
            raise ValueError(f"No flat_bets row with id {bet_id}.")
        cur = con.execute(
            "INSERT INTO annotations(bet_id, tag, reason, created_at) VALUES (?, ?, ?, ?)",
            (bet_id, tag, reason.strip(), datetime.now(timezone.utc).isoformat()),
        )
        return int(cur.lastrowid)


def annotated_bet_ids(store: Store) -> set[int]:
    with store.connect() as con:
        return annotated_ids(con)


def annotated_ids(con) -> set[int]:
    """Bet ids carrying ANY annotation (all tags) — the exclusion set every honest-variant /
    clean report reader consults so annotated rows (e.g. bug:settled-rebook-loop) never enter a
    P&L cell. Ensures the annotations table exists first, so readers are safe on a fresh DB.
    Read-only-safe: on a `mode=ro` connection (archive analysis) the CREATE is skipped and a
    missing table reads as no annotations."""
    try:
        con.executescript(SCHEMA)
    except sqlite3.OperationalError:
        pass  # read-only DB — table already exists (query works) or doesn't (→ empty set below)
    try:
        return {int(row["bet_id"]) for row in con.execute("SELECT DISTINCT bet_id FROM annotations")}
    except sqlite3.OperationalError:
        return set()
