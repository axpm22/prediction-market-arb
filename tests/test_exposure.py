import json
from datetime import datetime, timezone
from decimal import Decimal

from arb.exposure import compute_exposure
from arb.store import Store


def _bet(store: Store, kind: str, status: str, stake: str, venues: list[str]) -> None:
    legs = [{"venue": v, "book": "b", "outcome_label": "x", "cost": "0.5", "fee_cost": "0"} for v in venues]
    with store.connect() as con:
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, stake_usd, legs_json, status, placed_at, notes)
               VALUES (?, 1, 'sports', 'baseball_mlb', ?, 0, 0, '0.03', ?, ?, ?, ?, '')""",
            (f"fp{stake}{status}{venues}", kind, stake, json.dumps(legs), status,
             datetime.now(timezone.utc).isoformat()),
        )


def test_total_and_per_venue_exposure(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _bet(store, "EV", "OPEN", "30", ["kalshi"])
    _bet(store, "ARB", "CONFIRMED", "25", ["kalshi", "polymarket"])
    _bet(store, "EV", "SETTLED", "999", ["kalshi"])   # not active -> excluded
    _bet(store, "DIVERGENCE", "OPEN", "0", ["sx"])     # $0 stake -> excluded

    exp = compute_exposure(store, bankroll=Decimal("1000"), per_venue_cap=Decimal("40"))
    assert exp.total_committed == Decimal("55")        # 30 + 25
    assert exp.by_venue["kalshi"] == Decimal("55")     # both active bets touch kalshi
    assert exp.by_venue["polymarket"] == Decimal("25")
    assert not exp.over_bankroll
    assert exp.over_venue == ["kalshi"]                # 55 > 40 cap


def test_over_bankroll_flag(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _bet(store, "EV", "OPEN", "600", ["kalshi"])
    _bet(store, "EV", "OPEN", "600", ["polymarket"])
    exp = compute_exposure(store, bankroll=Decimal("1000"), per_venue_cap=Decimal("100"))
    assert exp.total_committed == Decimal("1200")
    assert exp.over_bankroll is True
