"""One-shot, idempotent DB migrations (pass3#7).

Currently: dedup true within-poll duplicate quotes and add the quotes UNIQUE index. Designed
to be safe on the live ledger — backs up first, dry-runs by default, and reports row counts
before/after. flat_bets is deliberately untouched (append-only; its one-open-per-fingerprint
invariant is enforced in app logic + poll atomicity, not a DB constraint).
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from .store import Store

_DUP_GROUPS_SQL = """
SELECT COUNT(*) FROM (
  SELECT 1 FROM quotes
   GROUP BY poll_id, event_key, venue, book, market_type, COALESCE(line,''), outcome
  HAVING COUNT(*) > 1
)
"""

# Delete all but the lowest id in each within-poll duplicate group (re-observations across
# polls have a different poll_id and are NOT duplicates, so they are preserved).
_DEDUP_SQL = """
DELETE FROM quotes
 WHERE id NOT IN (
   SELECT MIN(id) FROM quotes
    GROUP BY poll_id, event_key, venue, book, market_type, COALESCE(line,''), outcome
 )
"""


def backup_db(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, dest)
    return dest


def migrate_unique(store: Store, apply: bool) -> dict[str, int | str]:
    with store.connect() as con:
        before = con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        dup_groups = con.execute(_DUP_GROUPS_SQL).fetchone()[0]
    result: dict[str, int | str] = {"quotes_before": int(before), "dup_groups": int(dup_groups)}
    if not apply:
        result["mode"] = "dry-run"
        return result
    result["backup"] = str(backup_db(store.path))
    with store.transaction() as con:
        con.execute(_DEDUP_SQL)
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_unique "
            "ON quotes(poll_id, event_key, venue, book, market_type, line, outcome)"
        )
    with store.connect() as con:
        after = con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    result["mode"] = "applied"
    result["quotes_after"] = int(after)
    result["removed"] = int(before) - int(after)
    return result


def format_migration(r: dict[str, int | str]) -> str:
    lines = [f"migrate-unique [{r['mode']}]: {r['dup_groups']} within-poll duplicate group(s); "
             f"quotes before={r['quotes_before']}"]
    if r["mode"] == "applied":
        lines.append(f"  backup: {r['backup']}")
        lines.append(f"  removed {r['removed']} duplicate rows -> {r['quotes_after']} quotes; UNIQUE index ensured.")
    else:
        lines.append("  (dry-run — re-run with --apply to back up, dedup, and add the UNIQUE index.)")
    return "\n".join(lines)
