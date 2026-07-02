"""PURGE_LOOP_PNL: detector + append-only annotation + report-recompute exclusion.

Fixtures-free (synthetic ledger built in a temp Store); no live calls."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.experimental.analysis.loop_audit import (
    _matrix_cell, annotate_live, detect_loop_rows, loop_pnl, TAG,
)
from arb.experimental.annotations import annotated_bet_ids
from arb.store import Store

POLL = 60  # seconds
T0 = datetime(2026, 7, 2, 14, 0, tzinfo=timezone.utc)


def _legs(start: datetime) -> str:
    return json.dumps([{"event_key": "baseball_mlb:2026-07-02:a:b", "venue": "sx",
                        "market_type": "TOTAL", "outcome_label": "UNDER",
                        "start_utc": start.isoformat()}])


def _insert(con, *, fp, status, placed, settled=None, result=None, pnl="1.00",
            start=None, quality_flag=None, kind="EV"):
    con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, pnl_confirmed,
               status, placed_at, settled_at, result, quality_flag, notes)
           VALUES (?, 1, 'sports', 'baseball_mlb', ?, 0, 0, '0.05', '1.00', ?, '0.5', ?, ?, ?, ?, ?, ?, ?, '')""",
        (fp, kind, _legs(start or (T0 + timedelta(hours=6))),
         pnl, pnl, status, placed.isoformat(),
         settled.isoformat() if settled else None, result, quality_flag),
    )


def _build_ledger(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        # clean settled bet: placed pregame, settled hours later (normal latency)
        _insert(con, fp="clean", status="SETTLED", placed=T0, settled=T0 + timedelta(hours=6),
                result="WON", pnl="0.90", start=T0 + timedelta(hours=1))
        # 3-row WON loop chain: instant-settle, duplicate settlement
        for i in range(3):
            _insert(con, fp="loopwin", status="SETTLED", placed=T0 + timedelta(minutes=i),
                    settled=T0 + timedelta(minutes=i, seconds=1), result="WON", pnl="1.12",
                    start=T0 + timedelta(hours=9))  # nominal future start
        # 2-row LOST loop chain: instant-settle, duplicate settlement
        for i in range(2):
            _insert(con, fp="looplose", status="SETTLED", placed=T0 + timedelta(minutes=10 + i),
                    settled=T0 + timedelta(minutes=10 + i, seconds=1), result="LOST", pnl="-1.00",
                    start=T0 + timedelta(hours=9))
        # post-start single row: placed AFTER the (real, past) start, settled slowly
        _insert(con, fp="poststart", status="SETTLED", placed=T0 + timedelta(hours=2),
                settled=T0 + timedelta(hours=5), result="LOST", pnl="-1.00",
                start=T0 + timedelta(hours=1))
        # instant-settle single row (different fingerprint, not a duplicate)
        _insert(con, fp="instant", status="SETTLED", placed=T0 + timedelta(minutes=30),
                settled=T0 + timedelta(minutes=30, seconds=2), result="WON", pnl="0.80",
                start=T0 + timedelta(hours=9))
    return store


def test_detector_flags_exactly_loop_rows_clean_survives(tmp_path):
    store = _build_ledger(tmp_path)
    with store.connect() as con:
        findings = detect_loop_rows(con, POLL)
    flagged_fps = {f["fingerprint"] for f in findings}
    assert "clean" not in flagged_fps                        # the honest bet survives
    assert flagged_fps == {"loopwin", "looplose", "poststart", "instant"}
    # loopwin: 3 rows all flagged; looplose: 2; poststart: 1; instant: 1  => 7
    assert len(findings) == 7
    poststart = next(f for f in findings if f["fingerprint"] == "poststart")
    assert "post-start" in poststart["checks"]


def test_winner_and_loser_loops_excluded_symmetrically(tmp_path):
    # The integrity property: purging is sign-blind. Both the +$ WON chain and the -$ LOST chain
    # must be flagged; neither is spared for being profitable/unprofitable.
    store = _build_ledger(tmp_path)
    with store.connect() as con:
        findings = detect_loop_rows(con, POLL)
    by_result = {f["fingerprint"]: f["result"] for f in findings}
    assert by_result.get("loopwin") == "WON" and by_result.get("looplose") == "LOST"
    assert loop_pnl(findings) == Decimal("1.12") * 3 + Decimal("-1.00") * 2 + Decimal("-1.00") + Decimal("0.80")


def test_annotation_is_idempotent(tmp_path):
    store = _build_ledger(tmp_path)
    with store.connect() as con:
        findings = detect_loop_rows(con, POLL)
        first = annotate_live(con, findings)
        con.commit()
        second = annotate_live(con, findings)   # re-run: nothing new
        con.commit()
    assert first == 7 and second == 0
    assert len(annotated_bet_ids(store)) == 7


def test_report_recompute_excludes_annotated_from_every_cell(tmp_path):
    from arb.config import load_config
    from arb.experimental.dashboard import build_dashboard_data
    store = _build_ledger(tmp_path)
    config = load_config()
    with store.connect() as con:
        findings = detect_loop_rows(con, POLL)
        loop_ids = {f["id"] for f in findings}
        # Decimal-exact: the excluded raw-optimistic sum equals the loop rows' P&L, no drift.
        rows_before, pnl_before, rows_after, pnl_after = _matrix_cell(con, "pnl_optimistic", False, loop_ids)
        assert pnl_before - pnl_after == loop_pnl(findings)
        assert rows_before - rows_after == len(findings)
    # The dashboard reader honors the annotation: after annotating, the raw curve drops the loop
    # rows and only the clean bet's cumulative P&L remains.
    before = build_dashboard_data(store, config)
    with store.connect() as con:
        annotate_live(con, findings)
        con.commit()
    after = build_dashboard_data(store, config)
    before_final = before["equity"]["optimistic_raw"][-1]["cum"]
    after_final = after["equity"]["optimistic_raw"][-1]["cum"]
    assert round(before_final - after_final, 4) == float(loop_pnl(findings))
    assert round(after_final, 2) == 0.90  # only the clean bet survives
