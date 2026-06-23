from datetime import datetime, timezone
from pathlib import Path

from arb.experimental.analysis.devig_brier import score_methods, score_pairs
from arb.store import Store

FIX = Path("tests/fixtures")
EVENT = "baseball_mlb:2026-06-09:kansas city royals:texas rangers"  # KC beat TEX 5-3 in fixture


def test_score_pairs_rewards_calibration() -> None:
    # confident-and-right beats confident-and-wrong on Brier and log-loss.
    good = score_pairs([(0.9, True), (0.1, False)] * 10)
    bad = score_pairs([(0.9, False), (0.1, True)] * 10)
    assert good[1] < bad[1]   # lower Brier
    assert good[2] < bad[2]   # lower log-loss


def test_score_pairs_empty() -> None:
    assert score_pairs([]) == (0, 0.0, 0.0, 0.0)


def _book(con, book, kc_odds, tex_odds):
    for outcome, odds in (("Kansas City Royals", kc_odds), ("Texas Rangers", tex_odds)):
        con.execute(
            """INSERT INTO quotes(fetched_at, event_key, sport, venue, book, market_type, outcome,
                   raw_price, cost, fee_cost, live)
               VALUES (?, ?, 'baseball_mlb', 'oddsapi', ?, 'WINNER', ?, ?, ?, '0', 0)""",
            (datetime.now(timezone.utc).isoformat(), EVENT, book, outcome, str(odds), "0", ),
        )


def test_score_methods_ranks_devig_on_settled_event(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        # 3 books, KC the favorite (lower decimal odds). KC actually won (5-3).
        _book(con, "lowvig", "1.55", "2.55")
        _book(con, "betus", "1.60", "2.45")
        _book(con, "fanduel", "1.58", "2.50")
    scores = score_methods(store, min_books=3, fixture_dir=FIX)
    by = {s.method: s for s in scores}
    assert set(by) == {"proportional", "power", "shin"}
    # each method scored the 2 winner-consensus outcomes for the one settled event.
    assert all(s.n == 2 for s in scores)
    assert all(0.0 <= s.mean_brier <= 1.0 for s in scores)
