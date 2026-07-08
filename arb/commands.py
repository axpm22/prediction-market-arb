"""Command handlers. Phase 1: report renders the (empty) daily frame; the rest
arrive in their SPEC §16 phases and are absent until then."""
from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.table import Table

console = Console()


def cmd_report(cfg, store, args):
    day = args.daily or date.today().isoformat()
    bets = store.query(
        "SELECT * FROM flat_bets WHERE date(created_at) = ? ORDER BY id", (day,))
    polls = store.query(
        "SELECT COUNT(*) c, COALESCE(SUM(n_quotes),0) q FROM polls WHERE date(started_at) = ?",
        (day,))[0]
    table = Table(title=f"Daily report {day} — polls={polls['c']} quotes={polls['q']}")
    for col in ("kind", "lane", "bets", "staked", "pnl_optimistic"):
        table.add_column(col)
    if not bets:
        table.add_row("—", "—", "0", "$0.00", "$0.00")
    console.print(table)
    return 0
