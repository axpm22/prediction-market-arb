"""07 — Phase C: markout persist hook, summary, pnl_markout, flicker guard.

markout_at / markout_cents / pnl_markout / zombie_fillable exist (prompt 01); these lock the
persist hook + the read-time semantics on a synthetic quote stream.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.experimental.analysis.markout import (
    markout_at, markout_summary, persist_markouts, pnl_markout, zombie_fillable)
from arb.store import Store

T0 = datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc)
EK, VENUE, OUTCOME = "baseball_mlb:2026-06-09:a:b", "kalshi", "a"


def _seed(store: Store) -> None:
    with store.connect() as con:
        con.execute("INSERT INTO polls(started_at, kind) VALUES (?, 'watch')", (T0.isoformat(),))
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted,
                   live, edge, stake_usd, legs_json, optimistic_cost, status, placed_at, notes,
                   ai_matched, settlement_mismatch)
               VALUES ('fp', 1, 'sports', 'baseball_mlb', 'EV', 0, 0, '0.03', '1.00', ?, '0.50',
                       'OPEN', ?, '', 0, 0)""",
            (f'[{{"event_key":"{EK}","venue":"{VENUE}","outcome":"{OUTCOME}","cost":"0.50"}}]', T0.isoformat()))
        # quotes for the same outcome at +30s (0.52), +2m (0.55), +10m (0.60); NONE near +30m.
        for offset, cost in ((30, "0.52"), (120, "0.55"), (600, "0.60")):
            ts = (T0 + timedelta(seconds=offset)).isoformat()
            con.execute(
                """INSERT INTO quotes(fetched_at, event_key, category, sport, venue, book,
                       market_type, outcome, raw_price, cost, fee_cost, live, best_bid)
                   VALUES (?, ?, 'sports', 'baseball_mlb', ?, 'bk', 'WINNER', ?, ?, ?, '0', 0, NULL)""",
                (ts, EK, VENUE, OUTCOME, cost, cost))


def test_markout_at_finds_window_and_reports_gap(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed(store)
    m30 = markout_at(store, EK, VENUE, OUTCOME, T0, 30)
    assert m30 is not None and m30["ask"] == Decimal("0.52")
    assert markout_at(store, EK, VENUE, OUTCOME, T0, 1800) is None  # no quote near +30m → gap


def test_persist_markouts_upserts_and_records_null_gap(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed(store)
    assert persist_markouts(store) == 4  # 4 horizons for the one leg
    assert persist_markouts(store) == 4  # idempotent (UPSERT, no duplication)
    with store.connect() as con:
        rows = {r["horizon_secs"]: r["mid"] for r in con.execute(
            "SELECT horizon_secs, mid FROM markouts WHERE bet_id=1")}
    assert rows[30] == "0.52" and rows[120] == "0.55" and rows[600] == "0.60"
    assert rows[1800] is None  # the missing horizon is recorded NULL, never fetched specially
    # only one row per (bet, horizon) despite two persist runs
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM markouts").fetchone()[0] == 4


def test_markout_summary_signs(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed(store)
    summary = markout_summary(store)
    # entry 0.50; mid rose to 0.52/0.55/0.60 → positive markout (favourable drift here)
    assert summary[30]["n"] == 1 and summary[30]["median"] == Decimal("0.02")
    assert summary[600]["median"] == Decimal("0.10")
    assert summary[1800]["n"] == 0  # gap → excluded from stats


def test_pnl_markout_to_the_cent() -> None:
    # enter at the +30s re-quote 0.52; WON pays (1-0.52)/0.52 on $1
    assert pnl_markout(Decimal("0.52"), "WON", Decimal("1.00")) == Decimal("0.923077")
    assert pnl_markout(Decimal("0.52"), "LOST", Decimal("1.00")) == Decimal("-1.00")
    assert pnl_markout(Decimal("0.52"), "VOID", Decimal("1.00")) == Decimal("0")


def test_flicker_guard() -> None:
    # single appearance → flicker, not bookable
    assert zombie_fillable([{"poll_id": 1, "cost": "0.50"}]) == (False, "flicker")
    # two consecutive polls at the same price → bookable
    assert zombie_fillable([{"poll_id": 1, "cost": "0.50"}, {"poll_id": 2, "cost": "0.50"}]) == (True, None)
    # flash-verified single appearance → bookable
    assert zombie_fillable([{"poll_id": 1, "cost": "0.50"}], flash_verified=True) == (True, None)
