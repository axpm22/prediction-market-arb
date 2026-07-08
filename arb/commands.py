"""Command handlers. Phase 1: report renders the (empty) daily frame; the rest
arrive in their SPEC §16 phases and are absent until then."""
from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.table import Table

console = Console()


def cmd_watch(cfg, store, args):
    from arb.watch import fetch_all_venues
    quotes, status = fetch_all_venues(cfg)
    table = Table(title="watch --once" + (" --dry-run" if args.dry_run else ""))
    for col in ("venue", "ok", "quotes", "latency_ms"):
        table.add_column(col)
    for venue, st in status.items():
        table.add_row(venue, "✓" if st["ok"] else "✗ " + st.get("error", "")[:40],
                      str(st["n"]), f"{st['latency_ms']:.0f}")
    console.print(table)
    if args.dry_run:
        console.print("[dim]dry-run: nothing persisted[/]")
        return 0
    from arb.cycle import run_cycle
    return run_cycle(cfg, store, quotes, status, poll_kind="watch")


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
