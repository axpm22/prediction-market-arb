"""One-time historical quality backfill — BUILD_PLAN Phase 1.3.

Labels existing flat_bets rows that fail the quality guards. It NEVER deletes or changes a
bet's economic fields — it only sets the quality_flag label so the daily report can offer a
'clean' column beside the raw one (the parallel-column convention the pre-registration
mandates). Idempotent: re-running re-labels the same rows identically.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal

from arb.config import load_config
from arb.store import Store


def backfill(store: Store, min_executable_usd: Decimal, edge_suspect_above: Decimal) -> dict[str, int]:
    counts = {"suspect-edge": 0, "thin-book": 0, "clean": 0}
    with store.connect() as con:
        rows = list(con.execute("SELECT id, edge, legs_json FROM flat_bets WHERE kind IN ('EV','ARB','NEGRISK')"))
        for row in rows:
            flag = _classify_row(row, min_executable_usd, edge_suspect_above)
            counts[flag or "clean"] += 1
            con.execute("UPDATE flat_bets SET quality_flag = ? WHERE id = ?", (flag, row["id"]))
    return counts


def _classify_row(row, min_executable_usd: Decimal, edge_suspect_above: Decimal) -> str | None:
    try:
        edge = Decimal(str(row["edge"]))
    except Exception:  # noqa: BLE001
        edge = Decimal("0")
    if abs(edge) > edge_suspect_above:
        return "suspect-edge"
    for leg in json.loads(row["legs_json"] or "[]"):
        depth = leg.get("depth_usd")
        if depth is not None and Decimal(str(depth)) < min_executable_usd:
            return "thin-book"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arb.experimental.backfill_quality")
    parser.parse_args(argv)
    config = load_config()
    counts = backfill(Store(config.database_path), config.min_executable_usd, config.ev_edge_suspect_above)
    total = sum(counts.values())
    print(f"Quality backfill over {total} bets:")
    for flag, n in counts.items():
        print(f"  {flag:12s}: {n}")
    print("(labels only — no economic field was changed; append-only ledger intact)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
