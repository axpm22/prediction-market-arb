"""MODEL_FOUNDATIONS Phase C — markouts, pnl_markout, zombie/flicker guard (fixtures-free)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.experimental.analysis.markout import (
    markout_at, markout_cents, median, pnl_markout, zombie_fillable,
)
from arb.store import Store

ENTRY = datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc)


def _quote(con, cost, best_bid, when):
    con.execute(
        """INSERT INTO quotes(fetched_at, event_key, sport, venue, book, market_type, line,
               outcome, raw_price, cost, fee_cost, best_bid, live)
           VALUES (?, 'baseball_mlb:2026-07-02:a:b', 'baseball_mlb', 'kalshi', 'bk', 'WINNER', NULL,
                   'a', ?, ?, '0', ?, 0)""",
        (when.isoformat(), cost, cost, best_bid))


def test_markout_at_picks_nearest_quote_in_window(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        _quote(con, "0.55", "0.53", ENTRY + timedelta(seconds=25))   # near +30s target
        _quote(con, "0.60", "0.58", ENTRY + timedelta(seconds=200))  # far — outside +30s tol
    mo = markout_at(store, "baseball_mlb:2026-07-02:a:b", "kalshi", "a", ENTRY, 30)
    assert mo is not None
    assert mo["mid"] == Decimal("0.54")           # (0.53+0.55)/2
    # +30m horizon has no quote within tol -> None
    assert markout_at(store, "baseball_mlb:2026-07-02:a:b", "kalshi", "a", ENTRY, 1800) is None


def test_markout_cents_sign():
    assert markout_cents(Decimal("0.50"), Decimal("0.47")) == Decimal("-0.03")   # adverse
    assert markout_cents(Decimal("0.50"), Decimal("0.54")) == Decimal("0.04")    # favorable


def test_pnl_markout_uses_30s_entry():
    # entry at 0.50, WON: (1-0.5)/0.5 * 1 = 1.0
    assert pnl_markout(Decimal("0.50"), "WON", Decimal("1")) == Decimal("1.000000")
    assert pnl_markout(Decimal("0.50"), "LOST", Decimal("1")) == Decimal("-1")
    assert pnl_markout(Decimal("0.50"), "VOID", Decimal("1")) == Decimal("0")


def test_zombie_flicker_guard():
    # same price two consecutive polls -> fillable
    ok, tag = zombie_fillable([{"poll_id": 1, "cost": "0.50"}, {"poll_id": 2, "cost": "0.50"}])
    assert ok and tag is None
    # single appearance -> flicker, not fillable
    ok, tag = zombie_fillable([{"poll_id": 1, "cost": "0.50"}])
    assert not ok and tag == "flicker"
    # price moved each poll (never repeated) -> flicker, unless flash-verified
    appear = [{"poll_id": 1, "cost": "0.50"}, {"poll_id": 2, "cost": "0.51"}]
    assert zombie_fillable(appear) == (False, "flicker")
    assert zombie_fillable(appear, flash_verified=True) == (True, None)


def test_median():
    assert median([Decimal("1"), Decimal("3"), Decimal("2")]) == Decimal("2")
    assert median([Decimal("1"), Decimal("3")]) == Decimal("2")
    assert median([]) is None
