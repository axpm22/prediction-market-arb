"""The tape's one read-only open funnel, split out of arb/report_whale.py on
size (2026-09-05). `report_whale` re-exports `open_tape_ro` so every existing
`report_whale.open_tape_ro` / `from arb.report_whale import open_tape_ro`
caller (dashboard_whale.py, dashboard_whale_bundle.py, commands.py,
bench_whale.py, whale_state.py's docstring) keeps working unchanged.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from arb import whale_mat
from arb.replay import open_ro


def open_tape_ro(cfg) -> sqlite3.Connection | None:
    """The tape, read-only. None when the collector has never run here, or when
    the file cannot be opened read-only (a WAL DB under an active writer) — the
    section then reports what the ledger alone knows and says the tape is out.

    `positions_mat` is probed alongside `meta` (matpos 2026-09-05) because
    every reader through here goes on to call `score_state`, which reads that
    cache. The table is created by `whale_tape.open_tape`, which only a WRITER
    calls — so between deploying this and the lane's next cycle a tape can be
    perfectly readable and not yet carry it. Reporting "tape out", the degraded
    path this function already exists to serve, is the honest answer there; a
    bare `no such table` out of the daily report is not.

    Existing is not the same as CURRENT, though, and no reader here can make
    it current — so this is also where the cache's freshness is checked and
    warned about (I2, review of 783301f). One site, because every read-only
    reader passes through it and a per-reader check is a check a new reader
    forgets."""
    path = Path(str(cfg["whale"]["tape_db"]))
    if not path.exists():
        return None
    try:
        con = open_ro(path)
        con.execute("SELECT 1 FROM meta LIMIT 1")
        con.execute("SELECT 1 FROM positions_mat LIMIT 1")
        whale_mat.warn_if_stale(con, cfg)
        return con
    except sqlite3.Error:
        return None
