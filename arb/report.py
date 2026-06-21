from __future__ import annotations

from decimal import Decimal

from .models import Opportunity
from .store import Store

try:
    from rich.console import Console
    from rich.table import Table
except ModuleNotFoundError:  # pragma: no cover
    Console = None
    Table = None


def _pct(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"


def print_report(store: Store) -> None:
    summary = store.summary()
    if Console is None or Table is None:
        print("Report")
        for key, value in summary.items():
            print(f"{key}: {value}")
        return
    console = Console()
    table = Table(title="Paper Arbitrage Report")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for key, value in summary.items():
        table.add_row(key.replace("_", " ").title(), str(value))
    console.print(table)


def print_positions(store: Store) -> None:
    rows = store.list_positions()
    if Console is None or Table is None:
        if not rows:
            print("No positions.")
            return
        for row in rows:
            print(dict(row))
        return
    table = Table(title="Paper Positions")
    for column in ["ID", "Event", "Kind", "Cost", "Status", "PnL"]:
        table.add_column(column)
    for row in rows:
        table.add_row(str(row["id"]), row["event_key"], row["kind"], row["cost_usd"], row["status"], row["pnl_usd"] or "")
    Console().print(table)


def print_opportunities(opportunities: list[Opportunity], ev_display_threshold: Decimal | None = None) -> None:
    arbs = [opp for opp in opportunities if opp.kind == "ARB"]
    evs = [opp for opp in opportunities if opp.kind == "EV"]
    divergences = [opp for opp in opportunities if opp.kind == "DIVERGENCE"]
    # SPEC §1: ev_threshold gates the display only; every EV > 0 is still recorded.
    hidden_evs = 0
    if ev_display_threshold is not None:
        shown = [opp for opp in evs if opp.edge >= ev_display_threshold]
        hidden_evs = len(evs) - len(shown)
        evs = shown
    if Console is None or Table is None:
        print(f"TRUE ARB: {len(arbs)}")
        for opp in arbs:
            print(f"{opp.event.event_key} {opp.status} edge={opp.edge} notes={','.join(opp.notes)}")
        print(f"+EV: {len(evs)}")
        for opp in evs:
            print(f"{opp.event.event_key} {opp.status} edge={opp.edge} notes={','.join(opp.notes)}")
        print(f"DIVERGENCE: {len(divergences)}")
        for opp in divergences:
            print(f"{opp.event.event_key} {opp.status} spread={opp.edge} notes={','.join(opp.notes)}")
        if hidden_evs:
            print(f"({hidden_evs} +EV flags below display threshold; still recorded)")
        return
    console = Console()
    if hidden_evs:
        console.print(f"({hidden_evs} +EV flags below display threshold; still recorded)")
    for title, rows in [("TRUE ARB", arbs), ("+EV", evs), ("DIVERGENCE", divergences)]:
        table = Table(title=title)
        table.add_column("Event")
        table.add_column("Edge", justify="right")
        table.add_column("Status")
        table.add_column("Legs")
        table.add_column("Notes")
        for opp in rows:
            legs = ", ".join(f"{leg.outcome_label}@{leg.book}:{leg.effective_cost}" for leg in opp.legs)
            table.add_row(opp.event.event_key, _pct(opp.edge), opp.status, legs, ", ".join(opp.notes))
        console.print(table)
