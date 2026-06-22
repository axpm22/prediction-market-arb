from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Opportunity
from .pricing import money, q6
from .store import Store, quote_to_json
from .validation import ValidationRule, classify_validation

LIFECYCLE_STATUSES = {"OPEN", "CONFIRMED", "CLOSED", "SETTLED", "EXITED"}
# SPEC §9: P&L attributes to the EST/EDT day the bet was placed. placed_at is stored UTC,
# so a bet placed after ~19-20:00 ET rolls into the next UTC day; bucket by local date.
ATTRIBUTION_TZ = ZoneInfo("America/New_York")


def _local_date(placed_at: str | None) -> date | None:
    if not placed_at:
        return None
    try:
        dt = datetime.fromisoformat(str(placed_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ATTRIBUTION_TZ).date()


def record_tracker_poll(store: Store, poll_id: int, opportunities: list[Opportunity],
                        validation_rule: ValidationRule | None = None) -> None:
    validation_rule = validation_rule or ValidationRule()
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
                    validation_rule=validation_rule,
                )
            elif row["status"] == "OPEN" and int(row["opened_poll_id"]) != poll_id:
                _insert_tracker_row(
                    con,
                    poll_id,
                    current_opp=opp,
                    status="CONFIRMED",
                    reappearance_of=int(row["id"]),
                    notes="second-consecutive-observation",
                    validation_rule=validation_rule,
                )


def write_daily_report(store: Store, report_date: date | None = None, output_dir: Path = Path("reports/daily")) -> tuple[Path, Path]:
    report_date = report_date or datetime.now(ATTRIBUTION_TZ).date()
    output_dir.mkdir(parents=True, exist_ok=True)
    records = _bet_records_for_date(store, report_date)
    cumulative_records = _bet_records_for_date(store, None)
    csv_path = output_dir / f"{report_date.isoformat()}.csv"
    md_path = output_dir / f"{report_date.isoformat()}.md"
    with csv_path.open("w", newline="") as handle:
        fieldnames = [
            "id", "fingerprint", "kind", "category", "sport", "trusted", "live", "edge",
            "stake_usd", "optimistic_cost", "confirmed_cost", "pnl_optimistic",
            "pnl_confirmed", "pnl_kelly_optimistic", "pnl_kelly_confirmed",
            "result", "quality_flag", "kelly_f_star",
            "kelly_stake_usd", "validation_rule", "validation_tag", "verdicts", "notes",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(_csv_record(record))
    md_path.write_text(_daily_markdown(report_date, records, cumulative_records))
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
    # Flash verification and hot lifetime rows are annotations on an opportunity's
    # lifecycle, not lifecycle transitions. OPEN/CONFIRMED/CLOSED/SETTLED/EXITED rows decide
    # whether a fingerprint is still active or can reappear as a new bet.
    rows = list(con.execute("SELECT * FROM flat_bets ORDER BY id"))
    latest: dict[str, object] = {}
    for row in rows:
        if row["status"] in LIFECYCLE_STATUSES:
            latest[row["fingerprint"]] = row
    return {
        fingerprint: row
        for fingerprint, row in latest.items()
        if row["status"] in {"OPEN", "CONFIRMED"}
    }


def _latest_closed_id(con, fingerprint: str) -> int | None:
    row = con.execute(
        "SELECT id FROM flat_bets WHERE fingerprint = ? AND status IN ('CLOSED','EXITED') ORDER BY id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()
    return None if row is None else int(row["id"])


def _insert_tracker_row(con, poll_id: int, current_opp: Opportunity, status: str,
                        reappearance_of: int | None, notes: str,
                        validation_rule: ValidationRule | None = None) -> None:
    validation_rule = validation_rule or ValidationRule()
    cost = _opportunity_cost(current_opp)
    pnl = _optimistic_pnl(current_opp)
    confirmed_cost = cost if status == "CONFIRMED" else None
    confirmed_pnl = pnl if status == "CONFIRMED" else None
    rule_id, validation_tag, validation_notes = classify_validation(current_opp, status, validation_rule)
    ledger_notes = _merge_notes(notes, (*current_opp.notes, *validation_notes))
    stake_usd = _opportunity_stake(current_opp)
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, status, placed_at, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            str(stake_usd),
            json.dumps([quote_to_json(leg) for leg in current_opp.legs]),
            str(cost),
            None if confirmed_cost is None else str(confirmed_cost),
            None if pnl is None else str(pnl),
            None if confirmed_pnl is None else str(confirmed_pnl),
            status,
            datetime.now(timezone.utc).isoformat(),
            ledger_notes,
            current_opp.quality_flag,
            None if current_opp.kelly_f_star is None else str(current_opp.kelly_f_star),
            None if current_opp.kelly_stake is None else str(current_opp.kelly_stake),
            rule_id,
            validation_tag,
        ),
    )


def _merge_notes(primary: str, extra: tuple[str, ...]) -> str:
    notes: list[str] = []
    for note in (primary, *extra):
        if note and note not in notes:
            notes.append(note)
    return ",".join(notes)


def _opportunity_stake(opp: Opportunity) -> Decimal:
    if opp.kind == "DIVERGENCE":
        return Decimal("0")
    if opp.kind == "EV" and opp.legs:
        return opp.stakes.get(opp.legs[0].outcome_label, Decimal("1.00"))
    return Decimal("1.00")


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


def _bet_records_for_date(store: Store, report_date: date | None):
    # #1 Bucket by EST/EDT local date, not the UTC calendar date. SQLite can't tz-convert,
    # so filter in Python on the attribution-tz date of placed_at.
    with store.connect() as con:
        bases = list(con.execute(
            "SELECT * FROM flat_bets WHERE status = 'OPEN' AND kind != 'CLOSE' ORDER BY id"
        ))
        if report_date is not None:
            bases = [row for row in bases if _local_date(row["placed_at"]) == report_date]
        if not bases:
            return []
        ids = [int(row["id"]) for row in bases]
        placeholders = ",".join("?" for _ in ids)
        related_rows = list(
            con.execute(
                f"SELECT * FROM flat_bets WHERE reappearance_of IN ({placeholders}) ORDER BY id",
                ids,
            )
        )
    related: dict[int, list[object]] = defaultdict(list)
    for row in related_rows:
        related[int(row["reappearance_of"])].append(row)
    return [_bet_record(row, related.get(int(row["id"]), [])) for row in bases]


def _bet_record(base, related_rows: list[object]) -> dict[str, object]:
    by_status: dict[str, list[object]] = defaultdict(list)
    for row in related_rows:
        by_status[row["status"]].append(row)
    confirmed = by_status.get("CONFIRMED", [None])[0]
    settled = by_status.get("SETTLED", [None])[0]
    verdicts = [
        row["status"]
        for row in related_rows
        if row["status"] in {"VERIFIED", "VANISHED", "UNVERIFIABLE", "LIFETIME"}
    ]
    return {
        "base": base,
        "confirmed": confirmed,
        "settled": settled,
        "related": related_rows,
        "verdicts": verdicts,
    }


def _csv_record(record: dict[str, object]) -> dict[str, str | int | None]:
    base = record["base"]
    confirmed = record["confirmed"]
    settled = record["settled"]
    return {
        "id": base["id"],
        "fingerprint": base["fingerprint"],
        "kind": base["kind"],
        "category": base["category"],
        "sport": base["sport"],
        "trusted": base["trusted"],
        "live": base["live"],
        "edge": base["edge"],
        "stake_usd": base["stake_usd"],
        "optimistic_cost": base["optimistic_cost"],
        "confirmed_cost": None if confirmed is None else confirmed["confirmed_cost"],
        "pnl_optimistic": _record_pnl(record, "optimistic"),
        "pnl_confirmed": _record_pnl(record, "confirmed"),
        "pnl_kelly_optimistic": _record_pnl(record, "kelly_optimistic"),
        "pnl_kelly_confirmed": _record_pnl(record, "kelly_confirmed"),
        "result": None if settled is None else settled["result"],
        "quality_flag": base["quality_flag"],
        "kelly_f_star": base["kelly_f_star"],
        "kelly_stake_usd": base["kelly_stake_usd"],
        "validation_rule": _record_validation_rule(record),
        "validation_tag": _record_validation_tag(record),
        "verdicts": ",".join(record["verdicts"]),
        "notes": base["notes"],
    }


def _daily_markdown(report_date: date, records: list[dict[str, object]], cumulative_records: list[dict[str, object]]) -> str:
    headline = _headline_records(records)
    cumulative_headline = _headline_records(cumulative_records)
    lines = [
        f"# Daily Tracker Report {report_date.isoformat()}",
        "",
        "## Headline",
        "",
        _headline_line(headline),
        "",
        "## Class x Fill x Trust",
        "",
        *_table(
            ["Class", "Fill", "Trust", "Bets", "Staked", "Settled P&L", "Pending", "Win Rate", "ROI"],
            _class_fill_rows(records),
        ),
        "",
        "## Breakdowns",
        "",
        "### By Sport",
        "",
        *_table(["Sport", "Bets", "Optimistic P&L", "Confirmed P&L", "Pending"], _breakdown_rows(records, "sport")),
        "",
        "### By Category",
        "",
        *_table(["Category", "Bets", "Optimistic P&L", "Confirmed P&L", "Pending"], _breakdown_rows(records, "category")),
        "",
        "### By Venue Pair",
        "",
        *_table(["Venue Pair", "Bets", "Optimistic P&L", "Confirmed P&L", "Pending"], _venue_pair_rows(records)),
        "",
        "### EV Edge Buckets",
        "",
        *_table(["Bucket", "Bets", "Optimistic P&L", "Confirmed P&L", "Pending"], _ev_bucket_rows(records)),
        "",
        "### Validation Forward Test",
        "",
        *_table(["Rule", "Tag", "Bets", "Settled", "Settled P&L", "Pending"], _validation_rows(records)),
        "",
        "## Kelly",
        "",
        *_table(["Fill", "Scope", "Bets", "Staked", "Settled P&L", "Pending", "ROI"], _kelly_rows(records)),
        "",
        "## Extremes",
        "",
        *_table(["Metric", "Value", "Bet"], _extreme_rows(records)),
        "",
        "## Flagged vs Realized",
        "",
        *_table(["Class", "Flags", "Summed Flagged Edge", "Settled", "Optimistic P&L", "Confirmed P&L"], _flagged_vs_realized_rows(records)),
        "",
        "## Cumulative Headline",
        "",
        _headline_line(cumulative_headline),
        "",
        "## Left On The Table",
        "",
        "Not persisted yet. The watch loop prints filtered flags, but there is no append-only table for recomputing this section.",
        "",
        "---",
        "Scope: this ledger measures whether flagged prices were real and simultaneous.",
        "It does not measure capture - execution latency and partial fills are simulated",
        "(sequential edge in flash-verify rows); capital constraints are excluded.",
        "",
    ]
    return "\n".join(lines)


def _headline_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        record for record in records
        if int(record["base"]["trusted"]) == 1
        and int(record["base"]["live"]) == 0
        and record["base"]["category"] == "sports"
        and record["base"]["quality_flag"] is None
    ]


def _headline_line(records: list[dict[str, object]]) -> str:
    optimistic = _sum_pnl(records, "optimistic")
    confirmed = _sum_pnl(records, "confirmed")
    pending = sum(1 for record in records if record["settled"] is None)
    return (
        f"Bets: {len(records)} | Settled optimistic P&L: {_fmt_money(optimistic)} | "
        f"Settled confirmed P&L: {_fmt_money(confirmed)} | Pending: {pending}"
    )


def _class_fill_rows(records: list[dict[str, object]]) -> list[list[str]]:
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for record in records:
        base = record["base"]
        trust = "trusted" if int(base["trusted"]) else "review"
        for fill in ("optimistic", "confirmed"):
            grouped[(base["kind"], fill, trust)].append(record)
    rows: list[list[str]] = []
    for (kind, fill, trust), group in sorted(grouped.items()):
        settled = [record for record in group if _record_pnl(record, fill) is not None]
        pnl = _sum_pnl(settled, fill)
        staked = sum((_dec(record["base"]["stake_usd"]) for record in group), Decimal("0"))
        settled_staked = sum((_dec(record["base"]["stake_usd"]) for record in settled), Decimal("0"))
        roi = Decimal("0") if settled_staked == 0 else pnl / settled_staked
        wins = sum(1 for record in settled if record["settled"] is not None and record["settled"]["result"] == "WON")
        losses = sum(1 for record in settled if record["settled"] is not None and record["settled"]["result"] == "LOST")
        decided = wins + losses
        win_rate = "" if decided == 0 else _fmt_pct(Decimal(wins) / Decimal(decided))
        rows.append([
            kind, fill, trust, str(len(group)), _fmt_money(staked), _fmt_money(pnl),
            str(len(group) - len(settled)), win_rate, _fmt_pct(roi),
        ])
    return rows


def _pnl_pair_cells(group: list[dict[str, object]]) -> list[str]:
    # #8 every breakdown carries BOTH fills — the optimistic-vs-confirmed gap is the mirage.
    return [
        _fmt_money(_sum_pnl(group, "optimistic")),
        _fmt_money(_sum_pnl(group, "confirmed")),
        str(sum(1 for r in group if r["settled"] is None)),
    ]


def _breakdown_rows(records: list[dict[str, object]], field: str) -> list[list[str]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        grouped[str(record["base"][field] or "unknown")].append(record)
    return [
        [key, str(len(group)), *_pnl_pair_cells(group)]
        for key, group in sorted(grouped.items())
    ]


def _venue_pair_rows(records: list[dict[str, object]]) -> list[list[str]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        grouped[_venue_pair(record)].append(record)
    return [
        [key, str(len(group)), *_pnl_pair_cells(group)]
        for key, group in sorted(grouped.items())
    ]


def _ev_bucket_rows(records: list[dict[str, object]]) -> list[list[str]]:
    buckets = {"0-1%": [], "1-2%": [], "2-5%": [], ">5%": []}
    for record in records:
        if record["base"]["kind"] != "EV":
            continue
        edge = _dec(record["base"]["edge"])
        if edge < Decimal("0.01"):
            bucket = "0-1%"
        elif edge < Decimal("0.02"):
            bucket = "1-2%"
        elif edge < Decimal("0.05"):
            bucket = "2-5%"
        else:
            bucket = ">5%"
        buckets[bucket].append(record)
    return [
        [bucket, str(len(group)), *_pnl_pair_cells(group)]
        for bucket, group in buckets.items()
    ]


def _validation_rows(records: list[dict[str, object]]) -> list[list[str]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for record in records:
        rule = _record_validation_rule(record)
        tag = _record_validation_tag(record)
        if rule is None or tag is None:
            continue
        grouped[(rule, tag)].append(record)
    rows = []
    for (rule, tag), group in sorted(grouped.items()):
        settled = [record for record in group if _record_pnl(record, "confirmed") is not None]
        rows.append([
            rule,
            tag,
            str(len(group)),
            str(len(settled)),
            _fmt_money(_sum_pnl(settled, "confirmed")),
            str(len(group) - len(settled)),
        ])
    return rows


def _kelly_rows(records: list[dict[str, object]]) -> list[list[str]]:
    rows: list[list[str]] = []
    scopes = [
        ("raw", records),
        ("clean", [record for record in records if record["base"]["quality_flag"] is None]),
    ]
    for fill in ("kelly_optimistic", "kelly_confirmed"):
        for scope, group in scopes:
            sized = [record for record in group if _dec(record["base"]["kelly_stake_usd"]) > 0]
            settled = [record for record in sized if _record_pnl(record, fill) is not None]
            staked = sum((_dec(record["base"]["kelly_stake_usd"]) for record in sized), Decimal("0"))
            settled_staked = sum((_dec(record["base"]["kelly_stake_usd"]) for record in settled), Decimal("0"))
            pnl = _sum_pnl(settled, fill)
            roi = Decimal("0") if settled_staked == 0 else pnl / settled_staked
            rows.append([
                fill.replace("kelly_", ""),
                scope,
                str(len(sized)),
                _fmt_money(staked),
                _fmt_money(pnl),
                str(len(sized) - len(settled)),
                _fmt_pct(roi),
            ])
    return rows


def _extreme_rows(records: list[dict[str, object]]) -> list[list[str]]:
    settled = [(record, _record_pnl(record, "optimistic")) for record in records if _record_pnl(record, "optimistic") is not None]
    # #7 pnl_optimistic is None for everything but ARB (and ARBs settle at entry), so the old
    # pending metric was structurally dead. Expected pending P&L = stake x edge, where edge is
    # return-per-dollar for both EV (ratio) and ARB (ROI), so this surfaces real EV positions.
    pending = [
        (record, _pending_exposure(record))
        for record in records
        if record["settled"] is None
    ]
    rows: list[list[str]] = []
    if settled:
        best = max(settled, key=lambda item: item[1])
        worst = min(settled, key=lambda item: item[1])
        rows.append(["Biggest win", _fmt_money(best[1]), _bet_label(best[0])])
        rows.append(["Biggest loss", _fmt_money(worst[1]), _bet_label(worst[0])])
    if pending:
        largest = max(pending, key=lambda item: item[1])
        rows.append(["Largest unrealized pending", _fmt_money(largest[1]), _bet_label(largest[0])])
    return rows


def _pending_exposure(record: dict[str, object]) -> Decimal:
    base = record["base"]
    return q6(_dec(base["edge"]) * _dec(base["stake_usd"]))


def _flagged_vs_realized_rows(records: list[dict[str, object]]) -> list[list[str]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        grouped[str(record["base"]["kind"])].append(record)
    return [
        [
            kind,
            str(len(group)),
            _fmt_pct(sum((_dec(record["base"]["edge"]) for record in group), Decimal("0"))),
            str(sum(1 for record in group if _record_pnl(record, "optimistic") is not None)),
            _fmt_money(_sum_pnl(group, "optimistic")),
            _fmt_money(_sum_pnl(group, "confirmed")),
        ]
        for kind, group in sorted(grouped.items())
    ]


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["(none)"]
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *["| " + " | ".join(row) + " |" for row in rows],
    ]


def _sum_pnl(records: list[dict[str, object]], fill: str) -> Decimal:
    return sum((_record_pnl(record, fill) or Decimal("0") for record in records), Decimal("0"))


def _record_validation_rule(record: dict[str, object]) -> str | None:
    confirmed = record["confirmed"]
    base = record["base"]
    if confirmed is not None and confirmed["validation_rule"] is not None:
        return confirmed["validation_rule"]
    return base["validation_rule"]


def _record_validation_tag(record: dict[str, object]) -> str | None:
    confirmed = record["confirmed"]
    base = record["base"]
    if confirmed is not None and confirmed["validation_tag"] is not None:
        return confirmed["validation_tag"]
    return base["validation_tag"]


def _record_pnl(record: dict[str, object], fill: str) -> Decimal | None:
    settled = record["settled"]
    column = f"pnl_{fill}"
    if settled is not None and settled[column] is not None:
        return _dec(settled[column])
    return None


def _venue_pair(record: dict[str, object]) -> str:
    legs = json.loads(record["base"]["legs_json"])
    venues = sorted({str(leg.get("venue", "unknown")) for leg in legs})
    return "+".join(venues) if venues else "unknown"


def _bet_label(record: dict[str, object]) -> str:
    base = record["base"]
    return f"{base['id']} {base['kind']} {base['sport']}"


def _dec(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _fmt_money(value: Decimal) -> str:
    return f"${money(value)}"


def _fmt_pct(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"
