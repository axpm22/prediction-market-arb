"""Realized-P&L reads split out of arb/whale_metrics.py on size (2026-09-05):
the dashboard headline strip's Σ pnl tile and its 24h exit count. Same
ledger-only, tape-free reads as their old home — `arb.whale_metrics`
re-exports both (and `SETTLED_SQL`, needed by its own `roi_rows`/`exit_stats`)
so every existing caller keeps working unchanged.
"""
from __future__ import annotations

import json
from decimal import Decimal

from arb import metrics
from arb.models import q6

SETTLED_SQL = """
    SELECT b.* FROM flat_bets b WHERE b.kind='WHALE' AND EXISTS
      (SELECT 1 FROM bet_events e WHERE e.bet_id=b.id AND e.kind='settled')
    ORDER BY b.id"""


def realized_pnl(store) -> dict:
    """Σ dollar pnl over every settled WHALE bet row — every sizing variant
    (V*/flat, V*/conf, V*/kelly) is its own real paper stake, so all three
    count, unlike `roi_rows`' per-$1 ROI (grouped by root/variant for the CI
    tables below). The dashboard headline strip's first tile (2026-09-05);
    ledger-only, no tape, so deterministic per day like everything else here."""
    total, n = Decimal(0), 0
    for r in store.query(SETTLED_SQL):
        pnl = metrics.bet_pnl_optimistic(store, dict(r))
        if pnl is None:
            continue
        total += pnl
        n += 1
    return {"realized": q6(total), "realized_n": n}


def exits_recent(store, since) -> dict:
    """{"flip": n, "left": n} WHALE exits since `since` — the headline strip's
    24h count. `exit_stats` (whale_metrics.py) is the cumulative, per-root/
    reason table; this is a cheap separate windowed read of the same source
    column (a 24h count needs no root split)."""
    out = {"flip": 0, "left": 0}
    for r in store.query(
            """SELECT be.payload_json FROM bet_events be JOIN flat_bets b
                 ON b.id = be.bet_id
                WHERE b.kind='WHALE' AND be.kind='settled' AND be.created_at >= ?""",
            (since.isoformat(),)):
        source = str(json.loads(r["payload_json"]).get("source") or "")
        if source.startswith("exit:"):
            reason = source.split(":", 1)[1]
            out[reason] = out.get(reason, 0) + 1
    return out
