"""Phase 6 acceptance: simulated 3-poll fixture -> exact CSV; SQL headline
cross-check (dual implementation reserved for the one number that matters)."""
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.report import headline, write_daily
from arb.settlement import settle_manual
from arb.store import Store
from arb.tracker import process_poll

from tests.test_tracker import VENUE_OK, arb_opp, ev_opp

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg():
    c = load(ROOT / "config.yaml")
    c["ev_enabled"] = True
    return c


@pytest.fixture
def fixture_db(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 4, "n_opps": 2})
    process_poll(cfg, store, p1, "watch",
                 [arb_opp(cfg, "0.47", "0.49"), ev_opp(cfg, "0.50")], VENUE_OK, now=NOW)
    t2 = NOW + timedelta(seconds=60)
    p2 = store.insert("polls", {"started_at": t2.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p2, "watch", [arb_opp(cfg, "0.475", "0.495", fetched=t2)],
                 VENUE_OK, now=t2)
    settle_manual(cfg, store, "baseball_mlb:2026-07-08:mets:royals", "mets",
                  now=NOW + timedelta(hours=10))
    return store


def test_daily_csv_exact(cfg, fixture_db, tmp_path):
    frame = write_daily(cfg, fixture_db, "2026-07-08", outdir=tmp_path)
    rows = list(csv.DictReader((tmp_path / "2026-07-08.csv").open()))
    assert len(rows) == 2
    arb = [r for r in rows if r["kind"] == "ARB"][0]
    assert arb["pnl"] == "0.041667"
    assert arb["lane"] == "review"
    assert arb["confirmed"] == "True"
    assert arb["pnl_if_confirmed"] == "0.030928"   # 1/0.97 - 1, to the cent
    ev = [r for r in rows if r["kind"] == "EV"][0]
    assert ev["pnl"] == "0.932367"                  # (1-0.5175)/0.5175
    assert ev["confirmed"] == "False"               # missed -> $0 in the cell
    assert (tmp_path / "2026-07-08.md").exists()


def test_headline_matches_standalone_sql(cfg, fixture_db):
    hl, n = headline(cfg, fixture_db)
    assert n == 1 and hl == Decimal("0.932367")     # EV clean; ARB is review lane
    sql = (ROOT / "scripts" / "headline.sql").read_text()
    row = fixture_db.conn.execute(sql).fetchone()
    assert row["n_bets"] == n
    assert abs(row["headline_pnl"] - float(hl)) < 1e-6


def test_headline_sql_agrees_on_multileg(cfg, tmp_path):
    # metrics.py and headline.sql must agree on a MULTI-LEG settled bet (H2):
    # both read only per_leg[0] before the fix. 2-leg WON/LOST, costs 0.40/0.35
    # (sum 0.75), stake $0.75 -> contracts 1 -> 1*(1-0.40) - 1*0.35 = 0.25.
    import json
    store = Store(tmp_path / "ml.sqlite")
    legs = json.dumps([{"venue": "kalshi", "cost": "0.40", "side": "YES"},
                       {"venue": "kalshi", "cost": "0.35", "side": "NO"}])
    bet_id = store.insert("flat_bets", {
        "created_at": NOW.isoformat(), "kind": "ARB", "category": "sports",
        "lane": "clean", "trusted": 1, "event_start_utc": (NOW + timedelta(
            hours=6)).isoformat(), "legs_json": legs, "stake_usd": "0.75",
        "status": "OPEN"})
    store.insert("bet_events", {"bet_id": bet_id, "kind": "settled",
                                "payload_json": json.dumps(
                                    {"result": ["WON", "LOST"],
                                     "per_leg": ["WON", "LOST"]})})
    hl, n = headline(cfg, store)
    assert n == 1 and hl == Decimal("0.25")
    sql = (ROOT / "scripts" / "headline.sql").read_text()
    row = store.conn.execute(sql).fetchone()
    assert row["n_bets"] == 1
    assert abs(row["headline_pnl"] - 0.25) < 1e-6


def test_zero_flag_day_renders(cfg, tmp_path):
    store = Store(tmp_path / "empty.sqlite")
    store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                           "n_quotes": 100, "n_opps": 0})
    frame = write_daily(cfg, store, "2026-07-08", outdir=tmp_path)
    assert frame["by_class"] == {}
    assert frame["headline_n"] == 0
    assert (tmp_path / "2026-07-08.md").exists()
