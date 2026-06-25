"""Phase 8 (I1/I2) — the scorer must not zero-drag arbs, and must report confirmation rate.

An arb locks realized P&L at entry (pnl_optimistic) and always has NULL pnl_confirmed; the old
scorer treated NULL as $0, so every arb segment scored ~0 and could be wrongly BLOCKED. EV keeps
the confirmed-fill semantics (NULL -> 0 is correct there). confirmation_rate surfaces the real
finding (the fraction of a segment's scored P&L that is actually realized).
"""
from datetime import datetime, timezone

from arb.scoring import BLOCKED, score_segments
from arb.store import Store

CFG = {
    "weights": {"roi": 0.40, "trend": 0.25, "sample": 0.20, "win_rate": 0.15},
    "n_cap": 100, "score_min_n": 30, "block_below": 30, "recent_window": 10,
    "tiers": {"full": 80, "high": 60, "low": 40, "minimal": 30},
}


def _seed(store, sport, kind, n, win_rate, pnl_win, pnl_loss, *, confirmed):
    """Insert n settled clean bets. confirmed=False -> pnl_confirmed NULL (the arb / never-
    capturable case); the locked value always goes in pnl_optimistic."""
    wins = int(round(n * win_rate))
    with store.connect() as con:
        for i in range(n):
            won = i < wins
            pnl = pnl_win if won else pnl_loss
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, status, placed_at, result, pnl_optimistic,
                       pnl_confirmed, quality_flag, notes)
                   VALUES (?, 1, 'sports', ?, ?, 1, 0, '0.05', '1.00', '[]', 'SETTLED', ?, ?, ?, ?, NULL, '')""",
                (f"{sport}{kind}{i}", sport, kind, datetime.now(timezone.utc).isoformat(),
                 "WON" if won else "LOST", str(pnl), (str(pnl) if confirmed else None)),
            )


def test_arb_scored_on_lock_not_zeroed(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    # A profitable arb segment: every settled arb locked +0.03, pnl_confirmed always NULL.
    _seed(store, "baseball_mlb", "ARB", n=40, win_rate=1.0, pnl_win=0.03, pnl_loss=0.03, confirmed=False)
    [s] = [s for s in score_segments(store, CFG) if s.segment == "baseball_mlb:ARB"]
    assert s.roi > 0          # scored on the locked +0.03, not dragged to 0 by NULL confirmed
    assert s.tier != BLOCKED  # a profitable lock must not be blocked
    assert s.confirmation_rate == 1.0   # the lock is the realized-at-entry value


def test_ev_null_confirmed_still_zero_drags(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    # EV bets that never confirmed -> uncapturable -> 0 P&L correctly drags ROI (unchanged #6).
    _seed(store, "soccer", "EV", n=40, win_rate=1.0, pnl_win=0.90, pnl_loss=-1.0, confirmed=False)
    [s] = [s for s in score_segments(store, CFG) if s.segment == "soccer:EV"]
    assert s.roi == 0.0                 # all NULL confirmed -> zeroed
    assert s.confirmation_rate == 0.0   # the headline finding: nothing was capturable


def test_confirmation_rate_reports_partial(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed(store, "tennis", "EV", n=20, win_rate=1.0, pnl_win=0.50, pnl_loss=-1.0, confirmed=True)
    _seed(store, "tennis", "EV", n=20, win_rate=1.0, pnl_win=0.50, pnl_loss=-1.0, confirmed=False)
    [s] = [s for s in score_segments(store, CFG) if s.segment == "tennis:EV"]
    assert s.confirmation_rate == 0.5   # half the segment's P&L is real, half zeroed
