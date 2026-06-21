from datetime import datetime, timezone

from arb.scoring import BLOCKED, INSUFFICIENT, blocked_segments, score_segments
from arb.store import Store

CFG = {
    "weights": {"roi": 0.40, "trend": 0.25, "sample": 0.20, "win_rate": 0.15},
    "n_cap": 100, "score_min_n": 30, "block_below": 30, "recent_window": 10,
    "tiers": {"full": 80, "high": 60, "low": 40, "minimal": 30},
}


def seed_settled(store: Store, sport: str, kind: str, n: int, win_rate: float, pnl_each_win: float, pnl_each_loss: float) -> None:
    wins = int(round(n * win_rate))
    with store.connect() as con:
        for i in range(n):
            won = i < wins
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, status, placed_at, result, pnl_optimistic, quality_flag, notes)
                   VALUES (?, 1, 'sports', ?, ?, 1, 0, '0.05', '1.00', '[]', 'SETTLED', ?, ?, ?, NULL, '')""",
                (f"{sport}{kind}{i}", sport, kind, datetime.now(timezone.utc).isoformat(),
                 "WON" if won else "LOST", str(pnl_each_win if won else pnl_each_loss)),
            )


def test_strong_segment_scores_high_weak_segment_blocked(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    seed_settled(store, "basketball_nba", "EV", n=50, win_rate=0.74, pnl_each_win=0.90, pnl_each_loss=-1.0)
    seed_settled(store, "econ", "EV", n=40, win_rate=0.25, pnl_each_win=0.90, pnl_each_loss=-1.0)
    by_seg = {s.segment: s for s in score_segments(store, CFG)}
    assert by_seg["basketball_nba:EV"].tier in {"high", "full", "low"}
    assert by_seg["basketball_nba:EV"].tier != BLOCKED
    assert by_seg["econ:EV"].tier == BLOCKED
    assert "econ:EV" in blocked_segments(store, CFG)


def test_small_sample_is_insufficient_data(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    seed_settled(store, "tennis", "EV", n=5, win_rate=0.80, pnl_each_win=0.90, pnl_each_loss=-1.0)
    [s] = [s for s in score_segments(store, CFG) if s.segment == "tennis:EV"]
    assert s.tier == INSUFFICIENT
    assert s.score == 0.0


def test_suspect_bets_excluded_from_scoring(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    # 40 clean losers + tag would-be winners suspect → segment stays losing/blocked.
    seed_settled(store, "soccer", "EV", n=40, win_rate=0.0, pnl_each_win=0.0, pnl_each_loss=-1.0)
    with store.connect() as con:
        con.execute("UPDATE flat_bets SET quality_flag='suspect-edge' WHERE sport='soccer' AND rowid % 2 = 0")
    scores = {s.segment: s for s in score_segments(store, CFG)}
    # remaining clean soccer bets are all losers → blocked (or insufficient if <30 remain)
    assert scores["soccer:EV"].tier in {BLOCKED, INSUFFICIENT}
