"""SQLite layer: WAL, full schema, Decimal round-trip as TEXT."""
from decimal import Decimal

from arb import schema
from arb.store import Store

EXPECTED_TABLES = {
    "quotes", "polls", "poll_venues", "opportunities", "flat_bets", "bet_events",
    "annotations", "consensus", "consensus_books", "markouts", "heartbeats",
    "verdicts", "left_on_table", "blacklist", "schema_version",
}


def test_open_creates_full_schema(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    rows = store.query("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert EXPECTED_TABLES <= names


def test_wal_and_busy_timeout(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    assert store.query("PRAGMA journal_mode")[0][0] == "wal"
    assert store.query("PRAGMA busy_timeout")[0][0] == 5000


def test_decimal_round_trips_as_text(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    poll_id = store.insert("polls", {"started_at": "2026-07-08T00:00:00Z", "kind": "watch",
                                     "n_quotes": 0, "n_opps": 0})
    store.insert("quotes", {
        "poll_id": poll_id, "fetched_at": "2026-07-08T00:00:00Z", "event_key": "e",
        "category": "sports", "sport": "baseball_mlb", "venue": "kalshi", "book": "kalshi",
        "market_type": "WINNER", "line": None, "outcome": "yankees", "side": "YES",
        "raw_price": Decimal("0.470000"), "cost": Decimal("0.470000"),
        "fee_cost": Decimal("0.017433"), "depth_usd": Decimal("120.50"),
        "ladder_json": None, "live": 0,
    })
    row = store.query("SELECT cost, fee_cost FROM quotes")[0]
    assert row["cost"] == "0.470000"          # stored as TEXT, exact
    assert Decimal(row["fee_cost"]) == Decimal("0.017433")


def test_migrations_are_idempotent(tmp_path):
    p = tmp_path / "t.sqlite"
    Store(p)
    Store(p)  # re-opening re-runs migrations; must not fail
    assert schema.SCHEMA_VERSION >= 1
