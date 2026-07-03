from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .lifecycle import active_rows_via_index
from .models import Opportunity
from .pricing import money, q6
from .store import Store, quote_to_json
from .validation import DROP, ValidationRule, classify_validation
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
                        validation_rule: ValidationRule | None = None,
                        now: datetime | None = None,
                        only_kinds: tuple[str, ...] | None = None,
                        terminal_mute_grace_mins: int = 180,
                        failed_venues: set[str] | None = None,
                        dormant_sports: set[str] | None = None,
                        event_starts: dict[str, datetime] | None = None) -> None:
    """Record one poll's opportunities into the append-only ledger.

    `only_kinds` scopes the lifetime bookkeeping (the absent→closed and dedup logic) to a kind
    set. A separate cadence — e.g. parlay-scan running apart from `watch` — passes its own kind
    so it never closes the other stream's active fingerprints (they're simply not in this poll).

    `failed_venues` (K3): venues whose fetch failed/returned nothing this poll. A fingerprint
    whose legs touch one is EXEMPT from absent-closing — "venue didn't answer" is not "gone
    from the book", and closing it corrupts lifetimes with close/reappear storms.

    `dormant_sports`: sports skipped this poll for having no near-future fixtures. Their
    quotes never entered detection, so their fingerprints' absence proves nothing —
    exempt them exactly like a failed venue (no manufactured CLOSE storms).
    """
    validation_rule = validation_rule or ValidationRule()
    current = {opp.fingerprint: opp for opp in opportunities}
    now = now or datetime.now(timezone.utc)
    now = _as_utc(now)
    failed_venues = failed_venues or set()
    with store.connect() as con:
        active = _active_rows(con, only_kinds)
        active_positions = _active_position_keys(con, only_kinds)
        # Absent-close must only ever close fingerprints owned by THIS poll's cadence (the
        # poll-kind that opened the active row). Watch, focus, ev-exit, etc. interleave polls;
        # a fingerprint's absence from a different cadence's opportunity list proves nothing
        # (watch never emits focus's CLOSE/DIVERGENCE bets), and closing it churns the ledger
        # into duplicate close/reopen chains. only_kinds (bet-kind) can't separate them because
        # watch and focus BOTH emit DIVERGENCE — ownership is the poll cadence, not the bet kind.
        this_kind = _poll_kind(con, poll_id)
        owner_kind = _poll_kinds(con, {int(r["opened_poll_id"]) for r in active.values()})
        seen_positions: set[str] = set()
        for fingerprint, row in active.items():
            owned_by_this_cadence = (
                this_kind is None or owner_kind.get(int(row["opened_poll_id"])) == this_kind
            )
            if (owned_by_this_cadence
                    and fingerprint not in current
                    and not _touches_failed_venue(row, failed_venues)
                    and row["sport"] not in (dormant_sports or ())):
                _insert_close_row(con, poll_id, fingerprint, int(row["id"]), "absent-one-poll")
        for opp in opportunities:
            row = active.get(opp.fingerprint)
            rejection = None
            # Weakness #4 anti-inflation: don't re-record a fingerprint whose lifetime is
            # genuinely over. Checked BEFORE the in-play actionability gate so a finished+settled
            # fingerprint reads "terminal-fingerprint-skipped" (the accurate reason), not the
            # generic in-play skip. The mute is bounded two ways so it no longer swallows clean
            # flags: (a) only SETTLED counts (EXITED reappears as a new bet via _latest_closed_id),
            # and (b) it fires ONLY once the event has actually ended — a SETTLED row for a still-
            # future event is premature/own-settlement, so the re-sighting routes to the normal
            # open/reappearance path.
            if row is None:
                terminal = _latest_hard_terminal(con, opp.fingerprint)
                # A settle-tripwired fingerprint is blacklisted immediately (no event-over grace):
                # its outcome was already knowable at the tripwired settle, so any re-sighting is
                # the same knowable-outcome rebook the tripwire exists to stop (02-settle-tripwire).
                if terminal is not None and (
                    _fingerprint_tripwired(con, opp.fingerprint)
                    or _event_over(opp, now, terminal_mute_grace_mins)
                ):
                    rejection = (
                        f"skip-terminal-fingerprint:{terminal['status']}",
                        "terminal-fingerprint-skipped",
                        int(terminal["id"]),
                    )
            if rejection is None:
                rejection = _actionability_rejection(opp, now, event_starts)
                if rejection is not None and rejection[1] == "inplay-bet-refused":
                    print(f"inplay-bet-refused fingerprint={opp.fingerprint} event={opp.event.event_key}")
            if rejection is None and row is None:
                position_key = _position_key(opp)
                active_position = active_positions.get(position_key)
                if position_key in seen_positions:
                    rejection = (
                        f"skip-duplicate-position:{position_key}",
                        "duplicate-position-skipped",
                        None,
                    )
                elif active_position is not None:
                    rejection = (
                        f"skip-duplicate-position:{position_key}",
                        "duplicate-position-skipped",
                        int(active_position["id"]),
                    )
            if rejection is None and opp.quality_flag is None:
                tag = classify_validation(opp, "CONFIRMED" if row is not None and row["status"] == "OPEN" else "OPEN", validation_rule)[1]
                if tag == DROP:
                    rejection = ("skip-validation-drop", "validation-drop", int(row["id"]) if row is not None else None)
            if rejection is not None:
                note, quality_flag, parent_id = rejection
                if row is not None:
                    parent_id = int(row["id"])
                _insert_rejected_row(con, poll_id, opp, parent_id, note, quality_flag, validation_rule, now)
                continue
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
                    placed_at=now,
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
                    placed_at=now,
                )
            seen_positions.add(_position_key(opp))


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
    # Phase A premise monitor line — never let a scoring hiccup (or missing scores oracle) break
    # the report; the line is simply omitted if it can't be computed.
    premise = None
    try:
        from .experimental.analysis.regrade import rolling_brier
        premise = rolling_brier(store)
    except Exception:  # noqa: BLE001
        premise = None
    # Phase F cadence honesty — planned vs measured poll interval per loop, from the polls table.
    # The p95 printed here is the loop's REAL spec; "we scan every N s" claims cite it, not config.
    cadence = None
    try:
        from .config import load_config
        from .experimental.analysis.cadence import cadence_stats
        planned = {"watch": load_config().watch_interval_secs}
        cadence = [cadence_stats(store, kind, planned.get(kind)) for kind in ("watch", "focus", "ev-exit")]
        cadence = [c for c in cadence if c["n_gaps"] > 0]
    except Exception:  # noqa: BLE001
        cadence = None
    md_path.write_text(_daily_markdown(report_date, records, cumulative_records,
                                       store.load_left_on_table(report_date),
                                       _data_quality(store, report_date), premise=premise, cadence=cadence))
    return csv_path, md_path


def _premise_monitor_lines(premise: dict | None) -> list[str]:
    """Phase A premise monitor: one rolling line comparing consensus vs best-venue-mid Brier so the
    EV lane's core assumption (consensus = truth) stays continuously visible. Omitted when there is
    no settled consensus data yet. Pure formatter — the numbers come from regrade.rolling_brier."""
    if not premise or premise.get("consensus_brier") is None or premise.get("venue_brier") is None:
        return []
    cb, vb, n = premise["consensus_brier"], premise["venue_brier"], int(premise.get("n_consensus") or 0)
    verdict = "consensus sharper" if cb < vb else "venue mids sharper — EV premise inverted (Phase A)"
    return ["## Premise Monitor (Phase A)", "",
            f"- rolling consensus vs venue-mid Brier (n={n}): consensus {cb} vs venue {vb} — {verdict}",
            ""]


def _data_quality_lines(data_quality: dict[str, object] | None) -> list[str]:
    """The 'Data Quality' block for the daily report: voided (annotated) bets are out of every
    P&L cell above and shown here only, plus the in-play-refused counter (both zero => omitted)."""
    dq = data_quality or {}
    voided = dict(dq.get("voided") or {})
    refused = int(dq.get("inplay_refused") or 0)
    total_voided = sum(voided.values())
    if not total_voided and not refused:
        return []
    breakdown = ", ".join(f"{tag}={n}" for tag, n in sorted(voided.items())) or "none"
    return [
        "## Data Quality",
        "",
        f"- voided — data error: {total_voided} (excluded from all P&L; reasons: {breakdown})",
        f"- inplay-bet-refused: {refused} (bets blocked at the tracker boundary as in-play/unknown-start)",
        "",
    ]


def _data_quality(store: Store, report_date: date | None) -> dict[str, object]:
    """stop_inplay_betting_fix: the two safety counters the daily report surfaces so a voided
    incident and a refused in-play bet are never invisible — voided (annotated) bets by tag, and
    in-play-refused rows, both bucketed to the report's local date (all-time when report_date=None)."""
    from .experimental.annotations import annotated_ids

    voided: dict[str, int] = defaultdict(int)
    with store.connect() as con:
        annotated_ids(con)  # ensure the annotations table exists on a fresh DB
        for row in con.execute(
                "SELECT b.placed_at, a.tag FROM annotations a JOIN flat_bets b ON b.id = a.bet_id"):
            if report_date is None or _local_date(row["placed_at"]) == report_date:
                voided[str(row["tag"])] += 1
        refused = sum(
            1 for row in con.execute(
                "SELECT placed_at FROM flat_bets WHERE quality_flag = 'inplay-bet-refused'")
            if report_date is None or _local_date(row["placed_at"]) == report_date)
    return {"voided": dict(voided), "inplay_refused": refused}


def tracker_bet_view(store: Store) -> dict[str, dict[str, Decimal | None]]:
    from .experimental.annotations import annotated_ids
    view: dict[str, dict[str, Decimal | None]] = defaultdict(lambda: {"optimistic": None, "confirmed": None})
    with store.connect() as con:
        excluded = annotated_ids(con)  # PURGE_LOOP_PNL: annotated loop rows never enter the P&L view
        rows = [row for row in con.execute("SELECT * FROM flat_bets WHERE kind != 'CLOSE' ORDER BY id")
                if int(row["id"]) not in excluded]
    for row in rows:
        if row["status"] == "OPEN" and view[row["fingerprint"]]["optimistic"] is None and row["pnl_optimistic"]:
            view[row["fingerprint"]]["optimistic"] = Decimal(row["pnl_optimistic"])
        if row["status"] == "CONFIRMED" and view[row["fingerprint"]]["confirmed"] is None and row["pnl_confirmed"]:
            view[row["fingerprint"]]["confirmed"] = Decimal(row["pnl_confirmed"])
    return dict(view)


def _poll_kind(con, poll_id: int) -> str | None:
    row = con.execute("SELECT kind FROM polls WHERE id = ?", (poll_id,)).fetchone()
    return None if row is None else row["kind"]


def _poll_kinds(con, poll_ids: set[int]) -> dict[int, str]:
    """Map each poll id to its cadence (kind) in one query — O(1) per absent-close pass."""
    if not poll_ids:
        return {}
    placeholders = ",".join("?" * len(poll_ids))
    rows = con.execute(
        f"SELECT id, kind FROM polls WHERE id IN ({placeholders})", tuple(poll_ids)
    ).fetchall()
    return {int(r["id"]): r["kind"] for r in rows}


def _active_rows(con, only_kinds: tuple[str, ...] | None = None) -> dict[str, object]:
    # Flash verification and hot lifetime rows are annotations on an opportunity's
    # lifecycle, not lifecycle transitions. OPEN/CONFIRMED/CLOSED/SETTLED/EXITED rows decide
    # whether a fingerprint is still active or can reappear as a new bet.
    # S1: read only candidate-active rows via the fingerprint index (was SELECT * every poll).
    return active_rows_via_index(con, only_kinds)


def _active_position_keys(con, only_kinds: tuple[str, ...] | None = None) -> dict[str, object]:
    active = active_rows_via_index(con, only_kinds)  # S1: indexed, not a full flat_bets scan
    keyed: dict[str, object] = {}
    for row in active.values():
        key = _position_key_from_row(row)
        if key:
            keyed[key] = row
    return keyed


def _touches_failed_venue(row: Any, failed_venues: set[str]) -> bool:
    """K3: True when any of the bet's legs sits on a venue that failed this poll — the
    fingerprint's absence proves nothing, so it must not be absent-closed."""
    if not failed_venues:
        return False
    try:
        legs = json.loads(row["legs_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return False
    return any(str(leg.get("venue")) in failed_venues for leg in legs)


def _fingerprint_tripwired(con, fingerprint: str) -> bool:
    """True if any row for this fingerprint carries a `settle-tripwire` annotation — the instant-
    settle blacklist (02-settle-tripwire). Safe on a fresh DB (no annotations table yet)."""
    try:
        row = con.execute(
            """SELECT 1 FROM annotations a JOIN flat_bets b ON a.bet_id = b.id
                WHERE b.fingerprint = ? AND a.tag = 'settle-tripwire' LIMIT 1""",
            (fingerprint,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False  # annotations table absent → nothing blacklisted yet
    return row is not None


def _latest_hard_terminal(con, fingerprint: str):
    # Only SETTLED mutes — EXITED (an ev-exit take-profit/stop mid-life) must be allowed to
    # reappear as a new bet via _latest_closed_id, not be permanently silenced.
    return con.execute(
        """
        SELECT id, status FROM flat_bets
         WHERE fingerprint = ? AND status = 'SETTLED'
         ORDER BY id DESC LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()


def _event_over(opp: Opportunity, now: datetime, grace_mins: int) -> bool:
    """True only when the opportunity's event has genuinely ended — its (earliest) start is in
    the past by at least grace_mins. An unknown start is treated as NOT over (route to the normal
    reappearance path) so a missing time never permanently mutes a fingerprint."""
    starts = [opp.event.start_utc, *(leg.start_utc for leg in opp.legs)]
    known = [_as_utc(start) for start in starts if start is not None]
    if not known:
        return False
    return min(known) <= now - timedelta(minutes=grace_mins)


def _latest_closed_id(con, fingerprint: str) -> int | None:
    row = con.execute(
        "SELECT id FROM flat_bets WHERE fingerprint = ? AND status IN ('CLOSED','EXITED') ORDER BY id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()
    return None if row is None else int(row["id"])


def _actionability_rejection(opp: Opportunity, now: datetime,
                             event_starts: dict[str, datetime] | None = None) -> tuple[str, str, int | None] | None:
    # Belt-and-braces (stop_inplay_betting_fix): the last-line guard trusts the AUTHORITATIVE
    # event-level start (earliest reliable venue, unreliable venues excluded) over opp.event.start_utc,
    # which can be a mislabeled nominal slot (Polymarket tennis). A started or unknown reliable start
    # refuses the EV/ARB bet even when the per-opportunity start still looks pregame — the exact hole
    # that booked 340% in-play tennis EV. This is one invariant check, not a second routing system.
    if opp.kind in {"EV", "ARB"} and event_starts is not None:
        reliable = event_starts.get(opp.event.event_key)
        if reliable is None or _as_utc(reliable) <= now:
            return ("inplay-bet-refused", "inplay-bet-refused", None)
    starts = [leg.start_utc for leg in opp.legs]
    # PARLAY is pregame-only and never auto-filled. It must have at least one known leg start
    # (else it can't be validated pregame or settled by start time), and every known leg start
    # must be in the future — a parlay with any in-play leg is skipped. Nonsports parlays need
    # a manual resolver, like nonsports EV.
    if opp.kind == "PARLAY":
        leg_starts = [_as_utc(start) for start in starts if start is not None]
        if not leg_starts:
            return ("skip-missing-start", "missing-start-skipped", None)
        if any(start <= now for start in leg_starts):
            return ("skip-started-event", "in-play-skipped", None)
        if opp.category != "sports":
            return ("skip-nonsports-auto-action", "manual-resolver-required", None)
        return None
    if opp.kind in {"EV", "ARB"} and opp.event.start_utc is None:
        return ("skip-missing-start", "missing-start-skipped", None)
    # K9: event start can be None for non-EV/ARB kinds (never invented anymore).
    event_start = None if opp.event.start_utc is None else _as_utc(opp.event.start_utc)
    leg_starts = [_as_utc(start) for start in starts if start is not None]
    if opp.kind in {"EV", "ARB"} and (event_start <= now or any(start <= now for start in leg_starts)):
        return ("skip-started-event", "in-play-skipped", None)
    if opp.category != "sports" and opp.kind in {"EV", "ARB"} and not opp.event.ai_matched:
        # STRIKE_AWARE amendment: AI-matched nonsports pairs ARE paper-booked (the evidence
        # lane, settled by the same per-venue resolvers as sports arbs) — only human-mapped
        # nonsports stays manual-resolver-only. Both remain REVIEW/untrusted, out of the headline.
        return ("skip-nonsports-auto-action", "manual-resolver-required", None)
    return None


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _position_key(opp: Opportunity) -> str:
    leg_keys = [
        _leg_position_key(
            opp.event.event_key,
            leg.market_type,
            leg.line,
            leg.outcome_label,
        )
        for leg in opp.legs
    ]
    return f"{opp.category}|{opp.event.sport}|{opp.kind}|{'||'.join(sorted(leg_keys))}"


def _position_key_from_row(row: Any) -> str | None:
    try:
        legs = json.loads(row["legs_json"] or "[]")
    except json.JSONDecodeError:
        return None
    if not legs:
        return None
    leg_keys = [
        _leg_position_key(
            str(leg.get("event_key") or ""),
            str(leg.get("market_type") or ""),
            leg.get("line"),
            str(leg.get("outcome_label") or ""),
        )
        for leg in legs
    ]
    return f"{row['category']}|{row['sport']}|{row['kind']}|{'||'.join(sorted(leg_keys))}"


def _leg_position_key(event_key: str, market_type: str, line: object, outcome_label: str) -> str:
    return "|".join([event_key, market_type, "" if line is None else str(line), outcome_label.strip().lower()])


def _insert_tracker_row(con, poll_id: int, current_opp: Opportunity, status: str,
                        reappearance_of: int | None, notes: str,
                        validation_rule: ValidationRule | None = None,
                        placed_at: datetime | None = None) -> None:
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
                              live, edge, edge_points, adj_edge, p_hat, stake_usd, legs_json,
                              optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, status, placed_at, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag, ai_matched)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            None if current_opp.edge_points is None else str(current_opp.edge_points),
            None if current_opp.adj_edge is None else str(current_opp.adj_edge),
            None if current_opp.p_hat is None else str(current_opp.p_hat),
            str(stake_usd),
            json.dumps([quote_to_json(leg) for leg in current_opp.legs]),
            str(cost),
            None if confirmed_cost is None else str(confirmed_cost),
            None if pnl is None else str(pnl),
            None if confirmed_pnl is None else str(confirmed_pnl),
            status,
            (placed_at or datetime.now(timezone.utc)).isoformat(),
            ledger_notes,
            current_opp.quality_flag,
            None if current_opp.kelly_f_star is None else str(current_opp.kelly_f_star),
            None if current_opp.kelly_stake is None else str(current_opp.kelly_stake),
            rule_id,
            validation_tag,
            int(current_opp.event.ai_matched),
        ),
    )


def _insert_rejected_row(con, poll_id: int, current_opp: Opportunity, reappearance_of: int | None,
                         notes: str, quality_flag: str, validation_rule: ValidationRule | None = None,
                         placed_at: datetime | None = None) -> None:
    validation_rule = validation_rule or ValidationRule()
    cost = _opportunity_cost(current_opp)
    rule_id, validation_tag, validation_notes = classify_validation(current_opp, "OPEN", validation_rule)
    ledger_notes = _merge_notes(notes, (*current_opp.notes, *validation_notes))
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, edge_points, adj_edge, p_hat, stake_usd, legs_json,
                              optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, status, placed_at, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag, ai_matched)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, '0', ?, ?, NULL, NULL, NULL, 'CLOSED', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            current_opp.fingerprint,
            reappearance_of,
            poll_id,
            current_opp.category,
            current_opp.event.sport,
            current_opp.kind,
            int(any(leg.live for leg in current_opp.legs)) or int(quality_flag == "in-play-skipped"),
            str(current_opp.edge),
            None if current_opp.edge_points is None else str(current_opp.edge_points),
            None if current_opp.adj_edge is None else str(current_opp.adj_edge),
            None if current_opp.p_hat is None else str(current_opp.p_hat),
            json.dumps([quote_to_json(leg) for leg in current_opp.legs]),
            None if cost is None else str(cost),
            (placed_at or datetime.now(timezone.utc)).isoformat(),
            ledger_notes,
            quality_flag,
            None if current_opp.kelly_f_star is None else str(current_opp.kelly_f_star),
            None if current_opp.kelly_stake is None else str(current_opp.kelly_stake),
            rule_id,
            validation_tag,
            int(current_opp.event.ai_matched),
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
    # PARLAY: $1 flat on the basket (settles later as the AND of its legs).
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
    # PARLAY: legs[0] is the synthetic basket carrying the fee-adjusted ask (cost per $1 if
    # all legs win). P&L realizes at settlement, so optimistic_pnl is left pending like EV.
    if opp.kind == "PARLAY":
        return q6(opp.legs[0].effective_cost)
    return None


def _optimistic_pnl(opp: Opportunity) -> Decimal | None:
    cost = _opportunity_cost(opp)
    if cost is None or cost <= 0:
        return None
    if opp.kind == "ARB":
        # K10: store at q6 — cent-rounding booked every sub-0.5%-ROI arb as exactly $0.00
        # and erased the 0-1% edge band the harness exists to measure. Cents are display.
        return q6(Decimal("1") / cost - Decimal("1"))
    return None


def _bet_records_for_date(store: Store, report_date: date | None):
    # #1 Bucket by EST/EDT local date, not the UTC calendar date. SQLite can't tz-convert,
    # so filter in Python on the attribution-tz date of placed_at.
    from .experimental.annotations import annotated_ids
    with store.connect() as con:
        # PURGE_LOOP_PNL: exclude annotated rows (bug:settled-rebook-loop) from the report. The loop
        # P&L lives on annotated SETTLED children whose OPEN reappearance base isn't itself annotated,
        # so also drop bases whose fingerprint carries any annotation — otherwise the base survives
        # and re-books its loop child's P&L. Append-only: rows stay in flat_bets, only excluded here.
        excluded = annotated_ids(con)
        annotated_fps = {row["fingerprint"] for row in con.execute(
            "SELECT DISTINCT fingerprint FROM flat_bets WHERE id IN (SELECT bet_id FROM annotations)")}
        bases = list(con.execute(
            "SELECT * FROM flat_bets WHERE status = 'OPEN' AND kind != 'CLOSE' ORDER BY id"
        ))
        bases = [row for row in bases
                 if int(row["id"]) not in excluded and row["fingerprint"] not in annotated_fps]
        if report_date is not None:
            bases = [row for row in bases if _local_date(row["placed_at"]) == report_date]
        if not bases:
            return []
        ids = [int(row["id"]) for row in bases]
        placeholders = ",".join("?" for _ in ids)
        related_rows = [
            row for row in con.execute(
                f"SELECT * FROM flat_bets WHERE reappearance_of IN ({placeholders}) ORDER BY id",
                ids,
            )
            if int(row["id"]) not in excluded
        ]
    related: dict[int, list[object]] = defaultdict(list)
    for row in related_rows:
        related[int(row["reappearance_of"])].append(row)
    return [_bet_record(row, related.get(int(row["id"]), [])) for row in bases]


def _bet_record(base, related_rows: list[object]) -> dict[str, object]:
    by_status: dict[str, list[object]] = defaultdict(list)
    for row in related_rows:
        by_status[row["status"]].append(row)
    confirmed = by_status.get("CONFIRMED", [None])[0]
    # #1 EXITED (paper take-profit/stop) is a TERMINAL realized state, mutually exclusive
    # with SETTLED (settle and ev-exit each exclude the other's children). Treat it like a
    # settled row so the take-profit P&L is booked and the bet stops counting as pending.
    settled = by_status.get("SETTLED", [None])[0] or by_status.get("EXITED", [None])[0]
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


def _left_on_table_rows(rows: list[dict]) -> list[list[str]]:
    """Part 1: bucket the deliberately-not-booked EV candidates by reason (edge-implausible,
    outcome-unnormalized, fuzzy-no-exact-alias) with a count and summed suppressed edge."""
    buckets: dict[str, list[Decimal]] = defaultdict(list)
    for row in rows:
        edge = Decimal(str(row["edge"])) if row.get("edge") not in (None, "") else Decimal("0")
        buckets[row["reason"]].append(edge)
    return [[reason, str(len(edges)), str(sum(edges, Decimal("0")))]
            for reason, edges in sorted(buckets.items())]


def _cadence_lines(cadence: list[dict] | None) -> list[str]:
    """Phase F: planned vs measured poll interval per loop. The p95 is the loop's REAL spec."""
    if not cadence:
        return []
    from .experimental.analysis.cadence import format_cadence
    return ["## Cadence (Phase F — measured is the real spec, not config)", "",
            *(f"- {format_cadence(c)}" for c in cadence), ""]


def _daily_markdown(report_date: date, records: list[dict[str, object]], cumulative_records: list[dict[str, object]],
                    left_on_table: list[dict] | None = None, data_quality: dict[str, object] | None = None,
                    premise: dict | None = None, cadence: list[dict] | None = None) -> str:
    headline = _headline_records(records)
    cumulative_headline = _headline_records(cumulative_records)
    lines = [
        f"# Daily Tracker Report {report_date.isoformat()}",
        "",
        "## Headline",
        "",
        _headline_line(headline),
        "",
        *_premise_monitor_lines(premise),
        *_cadence_lines(cadence),
        *_data_quality_lines(data_quality),
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
        # Spec amendment (nonsports_ai_matcher): Haiku-matched bets in their own section,
        # never the headline. The flagged-vs-realized table is the Haiku error-rate monitor.
        "## AI-Matched (Haiku) — excluded from headline",
        "",
        *_table(
            ["Class", "Fill", "Trust", "Bets", "Staked", "Settled P&L", "Pending", "Win Rate", "ROI"],
            _class_fill_rows([r for r in records if _is_ai_matched(r)]),
        ),
        "",
        "### AI-Matched Flagged vs Realized (Haiku error-rate monitor)",
        "",
        *_table(
            ["Class", "Flags", "Summed Flagged Edge", "Settled", "Optimistic P&L", "Confirmed P&L"],
            _flagged_vs_realized_rows([r for r in records if _is_ai_matched(r)]),
        ),
        "",
        "## Cumulative Headline",
        "",
        _headline_line(cumulative_headline),
        "",
        "## Left On The Table",
        "",
        "EV candidates recorded but deliberately NOT booked (Part 1 correctness gates), by reason.",
        "",
        *(_table(["Reason", "Count", "Summed Suppressed Edge"], _left_on_table_rows(left_on_table or []))
          if left_on_table else ["_None today._", ""]),
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
        and not _is_ai_matched(record)  # spec amendment: AI-matched never in the headline
    ]


def _is_ai_matched(record: dict[str, object]) -> bool:
    try:
        return int(record["base"]["ai_matched"] or 0) == 1
    except (KeyError, IndexError):  # pre-migration rows
        return False


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
        # #4 Bucket on probability-points edge (cost-independent), not the (p-c)/c ratio,
        # which inflates for cheap contracts and scatters identical edges across buckets.
        edge = _edge_points(record["base"])
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


def _edge_points(base) -> Decimal:
    # Canonical probability-points edge; fall back to the legacy ratio for pre-migration rows.
    try:
        if base["edge_points"] not in (None, ""):
            return _dec(base["edge_points"])
    except (KeyError, IndexError):
        pass
    return _dec(base["edge"])


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
