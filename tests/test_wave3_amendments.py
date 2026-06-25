from datetime import datetime, timezone

from arb.scoring import score_segments, segment_calibration
from arb.store import Store


def _settled_ev(con, seg_sport, result, edge_points, cost, n, placed):
    for i in range(n):
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, edge_points, stake_usd, legs_json, optimistic_cost, status, placed_at,
                   settled_at, result, pnl_optimistic, pnl_confirmed, quality_flag, notes)
               VALUES (?, 1, 'sports', ?, 'EV', 1, 0, '0.05', ?, '1.00', '[]', ?, 'SETTLED', ?, ?, ?, ?, ?, NULL, '')""",
            (f"{seg_sport}{result}{i}{placed}", seg_sport, str(edge_points), str(cost),
             placed, placed, result, "0.9" if result == "WON" else "-1.0",
             "0.9" if result == "WON" else "-1.0"),
        )


# ---- EV#10: per-segment calibration gate ----

def test_segment_calibration_computes_ece(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        # p_hat = cost + edge_points = 0.50 + 0.10 = 0.60, but only 20% actually win ->
        # badly mis-calibrated (predicts 60%, realizes ~20%).
        _settled_ev(con, "baseball_mlb", "WON", "0.10", "0.50", 4, "2026-06-01T00:00:00+00:00")
        _settled_ev(con, "baseball_mlb", "LOST", "0.10", "0.50", 16, "2026-06-01T00:00:00+00:00")
        rows = [dict(r) for r in con.execute("SELECT * FROM flat_bets")]
    ece = segment_calibration({"baseball_mlb:EV": rows}, bins=10)
    assert ece["baseball_mlb:EV"] > 0.3   # ~0.40 gap between predicted 0.60 and realized 0.20


def test_calibration_gate_blocks_miscalibrated_segment(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        # High ROI (18-2) so the score gate PASSES, but p_hat=0.55 vs realized 0.90 -> ECE ~0.35,
        # so only the calibration gate should block it.
        _settled_ev(con, "baseball_mlb", "WON", "0.05", "0.50", 18, "2026-06-01T00:00:00+00:00")
        _settled_ev(con, "baseball_mlb", "LOST", "0.05", "0.50", 2, "2026-06-01T00:00:00+00:00")
    ungated = {s.segment: s.tier for s in score_segments(store, {"score_min_n": 5})}
    assert ungated["baseball_mlb:EV"] != "BLOCKED"   # ROI alone does not block it
    # B10: the calibration gate only fires once the segment clears ece_min_n (ECE on ~1/bin is
    # noise); this fixture has 20 rows, so set the floor low enough to exercise the gate.
    gated = {s.segment: s.tier for s in score_segments(
        store, {"score_min_n": 5, "calibration_gate": True, "ece_max": 0.10, "ece_min_n": 5})}
    assert gated["baseball_mlb:EV"] == "BLOCKED"      # calibration does


# ---- pass3#9: walk-forward holdout changes the fit set ----

def test_walk_forward_holdout_uses_fit_window(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        _settled_ev(con, "baseball_mlb", "WON", "0.05", "0.50", 40, "2026-06-01T00:00:00+00:00")
    full = {s.segment: s.n for s in score_segments(store, {"score_min_n": 5})}
    held = {s.segment: s.n for s in score_segments(store, {"score_min_n": 5, "walk_forward_holdout": 0.25})}
    assert held["baseball_mlb:EV"] < full["baseball_mlb:EV"]   # scored on the fit window only
