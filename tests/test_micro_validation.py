"""M5 — the micro lane is graded on its own realized ROI and stays quarantined until CI low > 0."""
from datetime import datetime, timezone

from arb.micro_validation import grade_micro_lane
from arb.store import Store


def _micro_bet(con, i, result, pnl, stake="10.00"):
    con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, status, placed_at,
               result, quality_flag, notes)
           VALUES (?, 1, 'sports', 'baseball_mlb', 'EV', 0, 0, '0.10', ?, '[]', '0.50', ?,
                   'SETTLED', ?, ?, 'micro', 'micro-ev-only')""",
        (f"m{i}", stake, str(pnl), datetime.now(timezone.utc).isoformat(), result),
    )


def _seed(tmp_path, bets):
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        for i, (result, pnl) in enumerate(bets):
            _micro_bet(con, i, result, pnl)
    return store


def test_micro_insufficient_sample_is_not_trustable(tmp_path):
    store = _seed(tmp_path, [("WON", "9.32")] * 5)
    g = grade_micro_lane(store, min_n=30)
    assert g.n == 5 and g.trustable is False and "INSUFFICIENT" in g.note


def test_micro_consistently_profitable_clears_the_ci_gate(tmp_path):
    store = _seed(tmp_path, [("WON", "9.32")] * 40)
    g = grade_micro_lane(store, min_n=30, resamples=2000)
    assert g.n == 40 and g.win_rate == 1.0
    assert g.roi_ci_low > 0 and g.trustable is True
    assert "TRUSTABLE" in g.note


def test_micro_with_losses_ci_includes_zero_stays_quarantined(tmp_path):
    # Alternating wins (+0.932 ROI) and full losses (-1.0 ROI) → mean ROI negative → CI low < 0.
    bets = [("WON", "9.32") if i % 2 == 0 else ("LOST", "-10.00") for i in range(40)]
    store = _seed(tmp_path, bets)
    g = grade_micro_lane(store, min_n=30, resamples=2000)
    assert g.n == 40 and g.decided == 40
    assert g.roi_ci_low <= 0 and g.trustable is False
    assert "QUARANTINED" in g.note
