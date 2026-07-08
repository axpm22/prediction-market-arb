"""SQLite WAL layer (SPEC §9). stdlib sqlite3, no ORM.

Decimal -> TEXT at the store boundary; bool -> INTEGER. Inserts batch in short
transactions. Single-writer-per-table discipline is procedural (poll-kind
ownership, §10.2) — the store provides only inserts and reads.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from arb import migrations
from arb.models import q6


def _adapt(v):
    if isinstance(v, Decimal):
        return str(q6(v))
    if isinstance(v, bool):
        return int(v)
    return v


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        migrations.apply(self.conn)

    def insert(self, table: str, row: dict) -> int:
        cols = ", ".join(row)
        ph = ", ".join("?" * len(row))
        cur = self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({ph})",
            [_adapt(v) for v in row.values()])
        self.conn.commit()
        return cur.lastrowid

    def insert_many(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = ", ".join(rows[0])
        ph = ", ".join("?" * len(rows[0]))
        self.conn.executemany(
            f"INSERT INTO {table} ({cols}) VALUES ({ph})",
            [[_adapt(v) for v in r.values()] for r in rows])
        self.conn.commit()

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def close(self):
        self.conn.close()
