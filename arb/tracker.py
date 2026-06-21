from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .models import Opportunity
from .pricing import money, q6
from .store import Store, quote_to_json


def record_tracker_poll(store: Store, poll_id: int, opportunities: list[Opportunity]) -> None:
    current = {opp.fingerprint: opp for opp in opportunities}
    with store.connect() as con:
        active = _active_rows(con)
        for fingerprint, row in active.items():
            if fingerprint not in current:
                _insert_close_row(con, poll_id, fingerprint, int(row["id"]), "absent-one-poll")
        for opp in opportunities:
            row = active.get(opp.fingerprint)
            if row is None:
                prior_closed = _latest_closed_id(con, opp.fingerprint)
                _insert_tracker_row(
                    con,
                    poll_id,
                    current_opp=opp,
                    status="OPEN",
                    reappearance_of=prior_closed,
                    notes="first-observed" if prior_closed is None else "reappeared",
                )
            elif row["status"] == "OPEN" and int(row["opened_poll_id"]) != poll_id:
                _insert_tracker_row(
                    con,
                    poll_id,
                    current_opp=opp,
                    status="CONFIRMED",
                    reappearance_of=int(row["id"]),
                    notes="second-consecutive-observation",
                )


def write_daily_report(store: Store, report_date: date | None = None, output_dir: Path = Path("reports/daily")) -> tuple[Path, Path]:
    report_date = report_date or datetime.now().date()
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = _tracker_rows_for_date(store, report_date)
    csv_path = output_dir / f"{report_date.isoformat()}.csv"
    md_path = output_dir / f"{report_date.isoformat()}.md"
    with csv_path.open("w", newline="") as handle:
        fieldnames = ["id", "fingerprint", "kind", "sport", "status", "trusted", "edge", "stake_usd", "pnl_optimistic", "pnl_confirmed", "notes"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row[name] for name in fieldnames})
    summary = _summarize(rows)
    md_path.write_text(
        "\n".join(
            [
                f"# Daily Tracker Report {report_date.isoformat()}",
                "",
                f"- Rows: {len(rows)}",
                f"- Trusted sports pregame rows: {summary['headline_count']}",
                f"- Optimistic P&L: {summary['pnl_optimistic']}",
                f"- Confirmed P&L: {summary['pnl_confirmed']}",
                "",
                "---",
                "Scope: this ledger measures whether flagged prices were real and simultaneous.",
                "It does not measure capture — execution latency and partial fills are simulated",
                "(sequential edge in flash-verify rows); capital constraints are excluded.",
                "",
            ]
        )
    )
    return csv_path, md_path


def tracker_bet_view(store: Store) -> dict[str, dict[str, Decimal | None]]:
    view: dict[str, dict[str, Decimal | None]] = defaultdict(lambda: {"optimistic": None, "confirmed": None})
    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets WHERE kind != 'CLOSE' ORDER BY id"))
    for row in rows:
        if row["status"] == "OPEN" and view[row["fingerprint"]]["optimistic"] is None and row["pnl_optimistic"]:
            view[row["fingerprint"]]["optimistic"] = Decimal(row["pnl_optimistic"])
        if row["status"] == "CONFIRMED" and view[row["fingerprint"]]["confirmed"] is None and row["pnl_confirmed"]:
            view[row["fingerprint"]]["confirmed"] = Decimal(row["pnl_confirmed"])
    return dict(view)


def _active_rows(con) -> dict[str, object]:
    rows = list(con.execute("SELECT * FROM flat_bets ORDER BY id"))
    latest: dict[str, object] = {}
    for row in rows:
        latest[row["fingerprint"]] = row
    return {
        fingerprint: row
        for fingerprint, row in latest.items()
        if row["status"] in {"OPEN", "CONFIRMED"}
    }


def _latest_closed_id(con, fingerprint: str) -> int | None:
    row = con.execute(
        "SELECT id FROM flat_bets WHERE fingerprint = ? AND status = 'CLOSED' ORDER BY id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()
    return None if row is None else int(row["id"])


def _insert_tracker_row(con, poll_id: int, current_opp: Opportunity, status: str, reappearance_of: int | None, notes: str) -> None:
    cost = _opportunity_cost(current_opp)
    pnl = _optimistic_pnl(current_opp)
    confirmed_cost = cost if status == "CONFIRMED" else None
    confirmed_pnl = pnl if status == "CONFIRMED" else None
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, status, placed_at, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            current_opp.fingerprint,
            reappearance_of,
            poll_id,
            current_opp.category,
            current_opp.event.sport,
            current_opp.kind,
            int(current_opp.status == "OK"),
            int(any(leg.live for leg in current_opp.legs)),
            str(current_opp.edge),
            "0" if current_opp.kind == "DIVERGENCE" else "1.00",
            json.dumps([quote_to_json(leg) for leg in current_opp.legs]),
            str(cost),
            None if confirmed_cost is None else str(confirmed_cost),
            None if pnl is None else str(pnl),
            None if confirmed_pnl is None else str(confirmed_pnl),
            status,
            datetime.now(timezone.utc).isoformat(),
            notes,
            current_opp.quality_flag,
            None if current_opp.kelly_f_star is None else str(current_opp.kelly_f_star),
            None if current_opp.kelly_stake is None else str(current_opp.kelly_stake),
        ),
    )


def _insert_close_row(con, poll_id: int, fingerprint: str, reappearance_of: int, notes: str) -> None:
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, status, placed_at, notes)
        VALUES (?, ?, ?, 'sports', '', 'CLOSE', 1, 0, '0', '0', '[]', 'CLOSED', ?, ?)
        """,
        (fingerprint, reappearance_of, poll_id, datetime.now(timezone.utc).isoformat(), notes),
    )


def _opportunity_cost(opp: Opportunity) -> Decimal | None:
    if opp.kind == "ARB":
        return q6(sum((leg.effective_cost for leg in opp.legs), Decimal("0")))
    if opp.kind == "EV":
        return q6(opp.legs[0].effective_cost)
    return None


def _optimistic_pnl(opp: Opportunity) -> Decimal | None:
    cost = _opportunity_cost(opp)
    if cost is None or cost <= 0:
        return None
    if opp.kind == "ARB":
        return money(Decimal("1") / cost - Decimal("1"))
    return None


def _tracker_rows_for_date(store: Store, report_date: date):
    with store.connect() as con:
        return list(
            con.execute(
                "SELECT * FROM flat_bets WHERE substr(placed_at, 1, 10) = ? ORDER BY id",
                (report_date.isoformat(),),
            )
        )


def _summarize(rows) -> dict[str, str | int]:
    headline = [row for row in rows if int(row["trusted"]) == 1 and int(row["live"]) == 0 and row["category"] == "sports"]
    optimistic = sum((Decimal(row["pnl_optimistic"]) for row in headline if row["pnl_optimistic"]), Decimal("0"))
    confirmed = sum((Decimal(row["pnl_confirmed"]) for row in headline if row["pnl_confirmed"]), Decimal("0"))
    return {
        "headline_count": len(headline),
        "pnl_optimistic": str(money(optimistic)),
        "pnl_confirmed": str(money(confirmed)),
    }
