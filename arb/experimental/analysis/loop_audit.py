"""PURGE_LOOP_PNL: one-shot detector for settled-rebook-loop rows (2026-07-02 incident).

A loop row is a bet placed after its outcome was already decided — never a forecast, regardless
of sign (wins and losses treated identically). Detection is check-based over the WHOLE ledger:

  1. duplicate-settlement — a fingerprint with >1 settled row; every settled row after the first
     is a loop row. The first settled row is a loop row only if it ALSO fails check 2 or 3.
  2. post-start        — placed_at >= the event's earliest leg start_utc (a pregame lane must
     never open after start).
  3. instant-settle    — settled_at - placed_at shorter than the lane's poll interval (the
     outcome was knowable at placement).

Live in-play lanes (LIVE_BASEBALL_EV) are exempt from checks 2 and 3 — they legitimately place
and settle inside the game. Annotation is append-only (tag `bug:settled-rebook-loop`) and only
ever touches the live DB; archives are scanned read-only and reported.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

TAG = "bug:settled-rebook-loop"
LIVE_KINDS = {"LIVE_BASEBALL_EV"}  # in-play lanes: place + settle mid-game is legitimate

ANNOTATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bet_id) REFERENCES flat_bets(id)
);
"""


def _parse_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _event_start(legs_json: str | None) -> datetime | None:
    """Earliest leg start_utc from the stored legs — the event's real start for A3 purposes."""
    try:
        legs = json.loads(legs_json or "[]")
    except (ValueError, TypeError):
        return None
    starts = [_parse_utc(leg.get("start_utc")) for leg in legs if isinstance(leg, dict)]
    known = [s for s in starts if s is not None]
    return min(known) if known else None


def detect_loop_rows(con: sqlite3.Connection, poll_interval_secs: float) -> list[dict]:
    """Return one dict per loop row: id, fingerprint, kind, placed_at, settled_at, quality_flag,
    result, pnl_optimistic, and `checks` (the check names that fired). Read-only."""
    rows = list(con.execute(
        """SELECT id, fingerprint, kind, status, placed_at, settled_at, quality_flag, result,
                  pnl_optimistic, legs_json
             FROM flat_bets ORDER BY id"""
    ))
    settled_by_fp: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["settled_at"] is not None:
            settled_by_fp.setdefault(row["fingerprint"], []).append(row)
    first_settled_id = {fp: rs[0]["id"] for fp, rs in settled_by_fp.items()}  # rows already id-ordered
    dup_fingerprints = {fp for fp, rs in settled_by_fp.items() if len(rs) > 1}

    findings: list[dict] = []
    for row in rows:
        live = str(row["kind"]) in LIVE_KINDS
        placed = _parse_utc(row["placed_at"])
        settled = _parse_utc(row["settled_at"])
        checks: list[str] = []
        if not live and settled is not None and placed is not None \
                and (settled - placed).total_seconds() < poll_interval_secs:
            checks.append("instant-settle")
        if not live:
            start = _event_start(row["legs_json"])
            if start is not None and placed is not None and placed >= start:
                checks.append("post-start")
        # duplicate-settlement: any settled row after the first for a fingerprint that settled >1x
        if row["settled_at"] is not None and row["fingerprint"] in dup_fingerprints \
                and row["id"] != first_settled_id[row["fingerprint"]]:
            checks.append("duplicate-settlement")
        if checks:
            findings.append({
                "id": int(row["id"]), "fingerprint": row["fingerprint"], "kind": row["kind"],
                "placed_at": row["placed_at"], "settled_at": row["settled_at"],
                "quality_flag": row["quality_flag"], "result": row["result"],
                "pnl_optimistic": row["pnl_optimistic"], "checks": checks,
            })
    return findings


def already_tagged_ids(con: sqlite3.Connection) -> set[int]:
    con.executescript(ANNOTATIONS_SCHEMA)
    return {int(r["bet_id"]) for r in con.execute(
        "SELECT DISTINCT bet_id FROM annotations WHERE tag = ?", (TAG,))}


def annotate_live(con: sqlite3.Connection, findings: list[dict]) -> int:
    """Append `bug:settled-rebook-loop` annotations for each finding. Idempotent: rows already
    carrying the tag are skipped. Append-only — no UPDATE/DELETE. Returns rows newly annotated."""
    con.executescript(ANNOTATIONS_SCHEMA)
    tagged = already_tagged_ids(con)
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    for finding in findings:
        if finding["id"] in tagged:
            continue
        con.execute(
            "INSERT INTO annotations(bet_id, tag, reason, created_at) VALUES (?, ?, ?, ?)",
            (finding["id"], TAG, "settled-rebook-loop: " + "+".join(finding["checks"]), now),
        )
        new += 1
    return new


def loop_pnl(findings: list[dict]) -> Decimal:
    """Total optimistic P&L carried by the loop rows (Decimal, no float drift)."""
    return sum((Decimal(str(f["pnl_optimistic"])) for f in findings if f["pnl_optimistic"] not in (None, "")),
               Decimal("0"))


# --------------------------------------------------------------------------------------------
# One-shot runner: detect (whole ledger + archives read-only), show delta, annotate live DB.
# --------------------------------------------------------------------------------------------
LIVE_DB = "data/arb.sqlite"
ARCHIVES = ["data/arb-v2-baseline-20260701.sqlite", "data/archive-pre-v2/arb.sqlite"]


def _sum_dec(con: sqlite3.Connection, where: str, params=()) -> Decimal:
    total = Decimal("0")
    for (val,) in con.execute(f"SELECT pnl_optimistic FROM flat_bets WHERE {where}", params):
        if val not in (None, ""):
            total += Decimal(str(val))
    return total


def _matrix_cell(con: sqlite3.Connection, pnl_col: str, clean: bool, loop_ids: set[int]) -> tuple[int, Decimal, int, Decimal]:
    """Return (rows_before, pnl_before, rows_after, pnl_after) for one honest-variant cell —
    settled rows summed on `pnl_col`, `clean` restricting to quality_flag IS NULL."""
    where = "status='SETTLED' AND result IN ('WON','LOST','VOID','RESOLVED')"
    if clean:
        where += " AND quality_flag IS NULL"
    rows_before, pnl_before, rows_after, pnl_after = 0, Decimal("0"), 0, Decimal("0")
    for row in con.execute(f"SELECT id, {pnl_col} AS p FROM flat_bets WHERE {where}"):
        val = Decimal(str(row["p"])) if row["p"] not in (None, "") else Decimal("0")
        rows_before += 1
        pnl_before += val
        if int(row["id"]) not in loop_ids:
            rows_after += 1
            pnl_after += val
    return rows_before, pnl_before, rows_after, pnl_after


def _print_delta(con: sqlite3.Connection, loop_ids: set[int]) -> None:
    print("\n=== Before/after (loop rows excluded) — honest-variant settled P&L ===")
    print(f"{'cell':<34}{'rows→':>10}{'pnl before':>14}{'pnl after':>14}")
    for basis, clean in [("optimistic/raw", False), ("optimistic/clean", True),
                         ("confirmed/raw", False), ("confirmed/clean", True)]:
        col = "pnl_optimistic" if basis.startswith("optimistic") else "pnl_confirmed"
        kcol = "pnl_kelly_optimistic" if basis.startswith("optimistic") else "pnl_kelly_confirmed"
        rb, pb, ra, pa = _matrix_cell(con, col, clean, loop_ids)
        krb, kpb, kra, kpa = _matrix_cell(con, kcol, clean, loop_ids)
        print(f"{basis+' / flat':<34}{f'{rb}->{ra}':>10}{str(pb):>14}{str(pa):>14}")
        print(f"{basis+' / kelly':<34}{f'{krb}->{kra}':>10}{str(kpb):>14}{str(kpa):>14}")
    today_before = _sum_dec(con, "status='SETTLED' AND substr(placed_at,1,10)='2026-07-02'")
    today_ids = {i for i in loop_ids}
    today_after = Decimal("0")
    for row in con.execute("SELECT id, pnl_optimistic p FROM flat_bets WHERE status='SETTLED' AND substr(placed_at,1,10)='2026-07-02'"):
        if int(row["id"]) not in today_ids and row["p"] not in (None, ""):
            today_after += Decimal(str(row["p"]))
    print(f"\nToday (2026-07-02) raw-optimistic settled P&L: {today_before}  ->  {today_after}")


def _scan_archives(poll_interval: float) -> None:
    from pathlib import Path
    print("\n=== Archives (read-only; reported, never annotated) ===")
    for path in ARCHIVES:
        if not Path(path).exists():
            print(f"  {path}: not present")
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            findings = detect_loop_rows(con, poll_interval)
            fps = len({f["fingerprint"] for f in findings})
            print(f"  {path}: {len(findings)} loop rows across {fps} fingerprints, "
                  f"optimistic P&L {loop_pnl(findings)}")
            con.close()
        except sqlite3.Error as exc:
            print(f"  {path}: unreadable ({exc})")


def main(argv=None) -> int:
    import argparse
    from arb.config import load_config
    parser = argparse.ArgumentParser(description="Detect + annotate settled-rebook-loop rows.")
    parser.add_argument("--apply", action="store_true", help="annotate the live DB (default: dry-run)")
    parser.add_argument("--db", default=LIVE_DB)
    args = parser.parse_args(argv)
    poll_interval = load_config().watch_interval_secs

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    before_count = con.execute("SELECT COUNT(*) FROM flat_bets").fetchone()[0]
    findings = detect_loop_rows(con, poll_interval)
    loop_ids = {f["id"] for f in findings}
    print(f"Loop rows detected in {args.db}: {len(findings)} "
          f"({len({f['fingerprint'] for f in findings})} fingerprints), "
          f"optimistic P&L {loop_pnl(findings)}")
    print(f"{'id':>6}  {'fingerprint':<16}{'kind':<8}{'flag':<12}{'result':<7}{'pnl':>10}  placed_at            checks")
    for f in findings:
        print(f"{f['id']:>6}  {str(f['fingerprint'])[:14]:<16}{str(f['kind']):<8}"
              f"{str(f['quality_flag'] or ''):<12}{str(f['result'] or ''):<7}"
              f"{str(f['pnl_optimistic'] or ''):>10}  {str(f['placed_at'])[:19]}  {'+'.join(f['checks'])}")
    _print_delta(con, loop_ids)
    _scan_archives(poll_interval)

    if args.apply:
        new = annotate_live(con, findings)
        con.commit()
        after_count = con.execute("SELECT COUNT(*) FROM flat_bets").fetchone()[0]
        annotated = len(already_tagged_ids(con))
        print(f"\nAnnotated {new} new rows (tag {TAG}); {annotated} rows now carry the tag.")
        print(f"flat_bets row count: {before_count} -> {after_count} "
              f"({'UNCHANGED — append-only honored' if before_count == after_count else 'CHANGED — BUG'})")
    else:
        print("\n(dry-run — nothing annotated; pass --apply to annotate the live DB)")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
