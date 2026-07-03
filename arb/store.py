from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from .models import Opportunity, Quote
from .schema import MIGRATION_COLUMNS, SCHEMA_SQL


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _dt_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        # Read-only mode (archive/oracle DBs): never create dirs, migrate, or set WAL — the file
        # is opened `mode=ro` so any accidental write raises. Used by `arb regrade --db <archive>`.
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
            con.row_factory = sqlite3.Row
            try:
                con.execute("PRAGMA busy_timeout=30000")
                yield con  # no commit — the connection cannot write
            finally:
                con.close()
            return
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    def init_db(self) -> None:
        with self.connect() as con:
            con.executescript(SCHEMA_SQL)
            self._migrate_existing_tables(con)
            self._ensure_unique_indexes(con)

    def _ensure_unique_indexes(self, con: sqlite3.Connection) -> None:
        # pass3#7: dedup integrity at the DB layer for QUOTES (true within-poll duplicates;
        # re-observations across polls have a different poll_id and stay allowed). Best-effort:
        # a dirty live DB (pre-dedup) must still open, so swallow the violation and warn —
        # `migrate-unique --apply` does the dedup then this succeeds. NOTE: flat_bets gets NO
        # such constraint — it is append-only with a DERIVED lifecycle (multiple OPEN rows
        # across reappearances are normal; "open" = latest lifecycle row, not the OPEN status),
        # so its one-open-per-fingerprint invariant is enforced in app logic + poll atomicity.
        try:
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_unique "
                "ON quotes(poll_id, event_key, venue, book, market_type, line, outcome)"
            )
        except sqlite3.IntegrityError:
            print("store: WARNING quotes have within-poll duplicates; run `migrate-unique --apply` to dedup.")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A single connection whose writes commit together (or roll back together on error).
        Use to make one poll's quote+consensus+finish writes atomic (pass3#7)."""
        with self.connect() as con:
            yield con

    def _migrate_existing_tables(self, con: sqlite3.Connection) -> None:
        for table, columns in MIGRATION_COLUMNS.items():
            self._ensure_columns(con, table, columns)
        for table in ("quotes", "opportunities"):
            self._relax_legacy_scan_id(con, table)

    def _relax_legacy_scan_id(self, con: sqlite3.Connection, table: str) -> None:
        # DBs created before polls existed declared scan_id NOT NULL; rebuild those
        # tables on the current schema, keeping every stored row.
        info = list(con.execute(f"PRAGMA table_info({table})"))
        scan_col = next((row for row in info if row["name"] == "scan_id"), None)
        if scan_col is None or not scan_col["notnull"]:
            return
        old_columns = [row["name"] for row in info]
        con.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        con.executescript(SCHEMA_SQL)
        new_columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
        shared = ", ".join(name for name in old_columns if name in new_columns)
        con.execute(f"INSERT INTO {table}({shared}) SELECT {shared} FROM {table}_legacy")
        con.execute(f"DROP TABLE {table}_legacy")

    def _ensure_columns(self, con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def start_poll(self, kind: str) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO polls(started_at, kind) VALUES (?, ?)",
                (_dt_text(datetime.now(timezone.utc)), kind),
            )
            return int(cur.lastrowid)

    def finish_poll(self, poll_id: int, credits_remaining: int | None, n_quotes: int, n_opps: int,
                    con: sqlite3.Connection | None = None) -> None:
        if con is None:
            with self.connect() as own:
                return self.finish_poll(poll_id, credits_remaining, n_quotes, n_opps, own)
        con.execute(
            """
            UPDATE polls
               SET finished_at = ?, credits_remaining = ?, n_quotes = ?, n_opps = ?
             WHERE id = ?
            """,
            (_dt_text(datetime.now(timezone.utc)), credits_remaining, n_quotes, n_opps, poll_id),
        )

    def save_scan_results(self, poll_id: int, quotes: list[Quote],
                          consensus: dict[tuple[str, str], Decimal], computed_at: datetime,
                          dispersion: dict[tuple[str, str], Decimal] | None,
                          credits_remaining: int | None, n_quotes: int, n_opps: int,
                          outcome_times: dict[tuple[str, str], datetime] | None = None) -> None:
        """pass3#7: one poll's quote + consensus + finish writes in ONE transaction, so a crash
        can't leave a half-written poll (they were three separate connections before)."""
        with self.transaction() as con:
            self.save_poll_quotes(poll_id, quotes, con)
            self.save_consensus(consensus, computed_at, dispersion, con, outcome_times=outcome_times)
            self.finish_poll(poll_id, credits_remaining, n_quotes, n_opps, con)

    def save_consensus(self, probabilities: dict[tuple[str, str], Decimal], computed_at: datetime,
                       dispersion: dict[tuple[str, str], Decimal] | None = None,
                       con: sqlite3.Connection | None = None,
                       outcome_times: dict[tuple[str, str], datetime] | None = None) -> None:
        if con is None:
            with self.connect() as own:
                return self.save_consensus(probabilities, computed_at, dispersion, own,
                                           outcome_times=outcome_times)
        dispersion = dispersion or {}
        # EV#6: stamp each row with the book's median line-move time when known, else scan clock.
        times = outcome_times or {}
        con.executemany(
                """
                INSERT INTO consensus(event_key, outcome, p_hat, computed_at, dispersion)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_key, outcome) DO UPDATE
                   SET p_hat = excluded.p_hat, computed_at = excluded.computed_at,
                       dispersion = excluded.dispersion
                """,
                [
                    (event_key, outcome, str(p_hat),
                     _dt_text(times.get((event_key, outcome), computed_at)),
                     None if (key := (event_key, outcome)) not in dispersion else str(dispersion[key]))
                    for (event_key, outcome), p_hat in probabilities.items()
                ],
            )

    def load_consensus(self) -> dict[tuple[str, str], tuple[Decimal, datetime]]:
        with self.connect() as con:
            rows = list(con.execute("SELECT event_key, outcome, p_hat, computed_at FROM consensus"))
        return {
            (row["event_key"], row["outcome"]): (Decimal(row["p_hat"]), datetime.fromisoformat(row["computed_at"]))
            for row in rows
        }

    def load_consensus_dispersion(self) -> dict[tuple[str, str], Decimal]:
        with self.connect() as con:
            rows = list(con.execute(
                "SELECT event_key, outcome, dispersion FROM consensus WHERE dispersion IS NOT NULL"))
        return {(row["event_key"], row["outcome"]): Decimal(row["dispersion"]) for row in rows}

    def save_poll_quotes(self, poll_id: int, quotes: list[Quote], con: sqlite3.Connection | None = None) -> None:
        if con is None:
            with self.connect() as own:
                return self.save_poll_quotes(poll_id, quotes, own)
        con.executemany(
                # OR IGNORE: the unique index drops true within-poll duplicates at write time.
                """
                INSERT OR IGNORE INTO quotes(poll_id, scan_id, fetched_at, event_key, category, sport, venue, book,
                                   market_type, line, outcome, raw_price, cost, fee_cost, depth_usd,
                                   max_size_usd, live, best_bid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        poll_id,
                        None,
                        _dt_text(quote.fetched_at),
                        quote.event_key,
                        "sports",
                        quote.sport,
                        quote.venue,
                        quote.book,
                        quote.market_type,
                        _decimal_text(quote.line),
                        quote.outcome_label,
                        str(quote.raw_price),
                        str(quote.cost),
                        str(quote.fee_cost),
                        _decimal_text(quote.depth_usd),
                        _decimal_text(quote.depth_usd),
                        int(quote.live),
                        _decimal_text(quote.best_bid),
                    )
                    for quote in quotes
                ],
            )

    def save_suspect_matches(self, poll_id: int, suspects: list[Opportunity]) -> None:
        # Quarantine, never delete: probable matching errors leave the divergence dataset
        # and wait here for a human to classify (alias bug / real dislocation / mismatch).
        with self.connect() as con:
            con.executemany(
                """
                INSERT INTO suspect_matches(poll_id, fingerprint, event_key, kind, edge, legs_json, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        poll_id, opp.fingerprint, opp.event.event_key, opp.kind, str(opp.edge),
                        json.dumps([quote_to_json(leg) for leg in opp.legs]), ",".join(opp.notes),
                        _dt_text(datetime.now(timezone.utc)),
                    )
                    for opp in suspects
                ],
            )

    def save_left_on_table(self, poll_id: int, rows: list[dict]) -> None:
        """Part 1: append the EV candidates a poll deliberately did NOT book (by reason), so the
        daily report can bucket them and nothing is a silent discard."""
        if not rows:
            return
        with self.connect() as con:
            con.executemany(
                """
                INSERT INTO left_on_table(poll_id, reason, sport, event_key, venue, outcome, edge, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (poll_id, row["reason"], row["sport"], row["event_key"], row.get("venue"),
                     row.get("outcome"), None if row.get("edge") is None else str(row["edge"]),
                     _dt_text(datetime.now(timezone.utc)))
                    for row in rows
                ],
            )

    def load_left_on_table(self, report_date=None) -> list[dict]:
        """Suppressed EV candidates for `report_date` (all if None). Used by the daily report's
        left-on-table section."""
        with self.connect() as con:
            if report_date is None:
                cursor = con.execute("SELECT reason, sport, event_key, venue, outcome, edge FROM left_on_table")
            else:
                cursor = con.execute(
                    "SELECT reason, sport, event_key, venue, outcome, edge FROM left_on_table "
                    "WHERE substr(created_at, 1, 10) = ?",
                    (report_date.isoformat(),),
                )
            return [dict(row) for row in cursor]

    def save_poll_opportunities(self, poll_id: int, opportunities: list[Opportunity]) -> None:
        with self.connect() as con:
            con.executemany(
                """
                INSERT INTO opportunities(poll_id, scan_id, fingerprint, kind, category, event_key, edge,
                                          status, legs_json, stakes_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        poll_id,
                        None,
                        opp.fingerprint,
                        opp.kind,
                        opp.category,
                        opp.event.event_key,
                        str(opp.edge),
                        opp.status,
                        json.dumps([quote_to_json(leg) for leg in opp.legs]),
                        json.dumps({key: str(value) for key, value in opp.stakes.items()}),
                        ",".join(opp.notes),
                    )
                    for opp in opportunities
                ],
            )

    def summary(self) -> dict[str, int | Decimal]:
        with self.connect() as con:
            polls = int(con.execute("SELECT COUNT(*) FROM polls").fetchone()[0])
            quotes = int(con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])
            opportunities = int(con.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
            flat_bets = int(con.execute("SELECT COUNT(*) FROM flat_bets").fetchone()[0])
            open_positions = int(con.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'").fetchone()[0])
        return {
            "polls": polls,
            "quotes": quotes,
            "opportunities": opportunities,
            "flat_bets": flat_bets,
            "open_positions": open_positions,
        }

    def list_positions(self) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(con.execute("SELECT * FROM positions ORDER BY id DESC"))

    def select_bets_where(self, where: str) -> list[sqlite3.Row]:
        """Rows matching a raw WHERE clause on flat_bets — the `arb annotate` admin preview.
        ponytail: raw SQL predicate, trusted operator-only local tool (no network surface)."""
        with self.connect() as con:
            return list(con.execute(
                f"SELECT id, sport, kind, status, placed_at, edge, notes FROM flat_bets "
                f"WHERE {where} ORDER BY id"))

    def sports_with_upcoming(self, now: datetime, lookahead_hours: int) -> set[str]:
        """Sports where a free venue recently listed a fixture dated within the lookahead
        window (event_key embeds the event date: `sport:YYYY-MM-DD:...`). The freshness
        bound reuses the same window: a listing not re-observed within it is presumed
        delisted. Lets `scan` gate Odds API spend on venue-derived activity without a
        venue fetch of its own — watch persists every quote each poll (rule #7)."""
        horizon = now + timedelta(hours=lookahead_hours)
        since = now - timedelta(hours=lookahead_hours)
        with self.connect() as con:
            rows = con.execute(
                """SELECT DISTINCT sport FROM quotes
                   WHERE venue != 'oddsapi' AND fetched_at >= ?
                     AND substr(event_key, instr(event_key, ':') + 1, 10) BETWEEN ? AND ?""",
                (_dt_text(since), now.date().isoformat(), horizon.date().isoformat()),
            ).fetchall()
        return {str(row["sport"]) for row in rows}


def quote_to_json(quote: Quote) -> dict[str, str | None]:
    return {
        "event_key": quote.event_key,
        "venue": quote.venue,
        "book": quote.book,
        "market_type": quote.market_type,
        "line": _decimal_text(quote.line),
        "outcome_label": quote.outcome_label,
        "raw_price": str(quote.raw_price),
        "cost": str(quote.cost),
        "fee_cost": str(quote.fee_cost),
        "depth_usd": _decimal_text(quote.depth_usd),
        "live": str(quote.live),
        "start_utc": None if quote.start_utc is None else _dt_text(quote.start_utc),
        "ref": quote.ref,
        # Wave 2: ladder stored as [[cost, depth], ...] so a bet leg keeps its fillable depth
        # for depth-capped P&L and offline slippage re-grade. Omitted when not captured.
        "ladder": None if quote.ladder is None else [[str(c), str(d)] for c, d in quote.ladder],
    }
