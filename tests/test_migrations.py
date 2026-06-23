from datetime import datetime, timezone

from arb.migrations import migrate_unique
from arb.store import Store


def _raw_quote(con, poll_id, outcome="a", qid=None):
    con.execute(
        """INSERT INTO quotes(id, poll_id, fetched_at, event_key, sport, venue, book, market_type,
               line, outcome, raw_price, cost, fee_cost, live)
           VALUES (?, ?, ?, 'e', 'baseball_mlb', 'oddsapi', 'dk', 'WINNER', NULL, ?, '2.0', '0.5', '0', 0)""",
        (qid, poll_id, datetime.now(timezone.utc).isoformat(), outcome),
    )


def test_migrate_unique_dedups_within_poll_keeps_reobservations(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    # Pre-insert a within-poll duplicate BEFORE the unique index exists (raw ids).
    with store.connect() as con:
        con.execute("DROP INDEX IF EXISTS idx_quotes_unique")
        _raw_quote(con, 1, "a", 1)
        _raw_quote(con, 1, "a", 2)   # same poll+outcome -> duplicate
        _raw_quote(con, 2, "a", 3)   # different poll -> a real re-observation, must survive

    dry = migrate_unique(store, apply=False)
    assert dry["dup_groups"] == 1 and dry["mode"] == "dry-run"

    applied = migrate_unique(store, apply=True)
    assert applied["removed"] == 1 and applied["quotes_after"] == 2
    with store.connect() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM quotes ORDER BY id")]
    assert ids == [1, 3]   # kept the lower-id dup and the cross-poll re-observation


def test_save_scan_results_is_atomic(tmp_path) -> None:
    from decimal import Decimal
    from arb.models import Quote
    store = Store(tmp_path / "arb.sqlite")
    q = Quote(event_key="baseball_mlb:2026-06-20:a:b", venue="kalshi", book="b", market_type="WINNER",
              line=None, outcome_label="a", raw_price=Decimal("0.5"), cost=Decimal("0.5"),
              fee_cost=Decimal("0"), depth_usd=None, live=False, fetched_at=datetime.now(timezone.utc))
    poll = store.start_poll("scan")
    store.save_scan_results(poll, [q], {("e", "a"): Decimal("0.55")}, datetime.now(timezone.utc),
                            {("e", "a"): Decimal("0.02")}, 400, 1, 0)
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM consensus").fetchone()[0] == 1
        assert con.execute("SELECT finished_at FROM polls WHERE id=?", (poll,)).fetchone()[0] is not None
        assert con.execute("SELECT dispersion FROM consensus").fetchone()[0] == "0.02"
