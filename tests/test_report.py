"""Phase 6 acceptance: simulated 3-poll fixture -> exact CSV; SQL headline
cross-check (dual implementation reserved for the one number that matters)."""
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.report import daily_frame, headline, write_daily
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


def test_daily_boundary_is_config_timezone_not_utc(cfg, tmp_path):
    # H5: config timezone is America/New_York. A bet created 01:00 UTC is 21:00
    # EDT the PREVIOUS day, so it belongs to the 2026-07-08 ET report, not 07-09.
    import json
    store = Store(tmp_path / "tz.sqlite")
    store.insert("flat_bets", {
        "created_at": "2026-07-09T01:00:00+00:00", "kind": "EV",
        "category": "sports", "lane": "review", "trusted": 0, "stake_usd": "1",
        "status": "OPEN", "legs_json": json.dumps(
            [{"venue": "kalshi", "cost": "0.5", "outcome": "mets", "side": "YES"}])})
    ids_0708 = [b["id"] for b in daily_frame(cfg, store, "2026-07-08")["per_bet"]]
    ids_0709 = [b["id"] for b in daily_frame(cfg, store, "2026-07-09")["per_bet"]]
    assert len(ids_0708) == 1        # lands on the ET day it was placed
    assert ids_0709 == []            # NOT the following UTC day


def test_zero_flag_day_renders(cfg, tmp_path):
    store = Store(tmp_path / "empty.sqlite")
    store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                           "n_quotes": 100, "n_opps": 0})
    frame = write_daily(cfg, store, "2026-07-08", outdir=tmp_path)
    assert frame["by_class"] == {}
    assert frame["headline_n"] == 0
    assert (tmp_path / "2026-07-08.md").exists()


def test_headline_sql_agrees_on_unresolved_leg(cfg, tmp_path):
    # D4 (review 2026-07-10): a settled event carrying an unresolved leg is
    # PENDING in metrics (excluded from the headline); the SQL's ELSE 0 counted
    # the bet with a partial per-leg sum. Both must exclude it.
    import json
    store = Store(tmp_path / "ur.sqlite")
    legs = json.dumps([{"venue": "kalshi", "cost": "0.40", "side": "YES"},
                       {"venue": "kalshi", "cost": "0.35", "side": "NO"}])
    bet_id = store.insert("flat_bets", {
        "created_at": NOW.isoformat(), "kind": "EV", "category": "sports",
        "lane": "clean", "trusted": 1, "event_start_utc": (NOW + timedelta(
            hours=6)).isoformat(), "legs_json": legs, "stake_usd": "0.75",
        "status": "OPEN"})
    store.insert("bet_events", {"bet_id": bet_id, "kind": "settled",
                                "payload_json": json.dumps(
                                    {"result": ["WON", "PENDING"],
                                     "per_leg": ["WON", "PENDING"]})})
    hl, n = headline(cfg, store)
    assert n == 0 and hl == Decimal("0")
    sql = (ROOT / "scripts" / "headline.sql").read_text()
    row = store.conn.execute(sql).fetchone()
    assert row["n_bets"] == 0
    assert row["headline_pnl"] == 0


def test_write_daily_deterministic_across_wall_clock(cfg, fixture_db, tmp_path,
                                                      monkeypatch):
    # §H14 flake fix: a day's report is a pure function of the stored data and
    # `day` (rule 9) — nothing on the write_daily path may read the wall clock.
    # A `datetime.now()` that ticks forward on every call poisons the hash if
    # anything still reads real time; after the fix it is never called, so two
    # calls with different mocked clocks must still render byte-identical md.
    import arb.report as report_mod
    import arb.report_whale as report_whale_mod
    import arb.whale_kill as whale_kill_mod

    calls = {"n": 0}
    base = datetime(2099, 1, 1, tzinfo=timezone.utc)  # far from any real "now" use

    class TickingDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            calls["n"] += 1
            return base + timedelta(seconds=2 * calls["n"])

    for mod in (report_mod, report_whale_mod, whale_kill_mod):
        monkeypatch.setattr(mod, "datetime", TickingDatetime)

    write_daily(cfg, fixture_db, "2026-07-08", outdir=tmp_path)
    md1 = (tmp_path / "2026-07-08.md").read_text()
    write_daily(cfg, fixture_db, "2026-07-08", outdir=tmp_path)
    md2 = (tmp_path / "2026-07-08.md").read_text()

    assert calls["n"] == 0, "write_daily read the wall clock instead of day_bounds"
    assert md1 == md2
    lines = (tmp_path / "HASHES.log").read_text().strip().splitlines()
    assert len(lines) == 2 and lines[0] == lines[1]


def test_write_daily_hash_ignores_collector_churn_and_future_tape_rows(
        cfg, fixture_db, tmp_path):
    # regrade 2026-08-29: the whale collector rewrites meta.last_cycle on its
    # own ~60s cadence and can append tape rows after this report day has
    # already closed (a real collector never stops for a past day's report).
    # Neither may change a past day's hash — the md must stay a pure function
    # of the report day and the tape rows up to its end (rule 9). Regression
    # for the flake in test_write_daily_appends_hash_chain: that test alone
    # cannot catch this because conftest's tape redirection makes both of its
    # calls read the same untouched (empty) tape either way.
    import json

    from arb import metrics
    from arb.whale_tape import _set_meta, open_tape

    day_end = datetime.fromisoformat(metrics.day_bounds(cfg, "2026-07-08")[1])
    future = day_end + timedelta(hours=1)
    con = open_tape(cfg["whale"]["tape_db"])
    con.close()

    write_daily(cfg, fixture_db, "2026-07-08", outdir=tmp_path)
    lines = (tmp_path / "HASHES.log").read_text().strip().splitlines()
    assert len(lines) == 1

    con = open_tape(cfg["whale"]["tape_db"])
    _set_meta(con, "last_cycle", json.dumps(
        {"status": "error", "prints": 999, "bad_rows": 999, "at": future.isoformat()}))
    con.execute(
        "INSERT INTO prints (observed_at, ts, proxy_wallet, condition_id, "
        "asset, outcome_index, side, price, size, notional_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (future.isoformat(), int(future.timestamp()), "wX", "0xcond",
         "0xyes", 0, "BUY", "0.50", "1000", "500"))
    con.commit()
    con.close()

    write_daily(cfg, fixture_db, "2026-07-08", outdir=tmp_path)
    lines = (tmp_path / "HASHES.log").read_text().strip().splitlines()
    assert len(lines) == 2 and lines[0] == lines[1]


def test_write_daily_appends_hash_chain(cfg, fixture_db, tmp_path):
    # verifiability: every write appends (never rewrites) the day's report
    # hashes; an idempotent recompute reproduces the same hash
    import hashlib
    write_daily(cfg, fixture_db, "2026-07-08", outdir=tmp_path)
    write_daily(cfg, fixture_db, "2026-07-08", outdir=tmp_path)
    lines = (tmp_path / "HASHES.log").read_text().strip().splitlines()
    assert len(lines) == 2 and lines[0] == lines[1]  # append-only, reproducible
    want = hashlib.sha256((tmp_path / "2026-07-08.md").read_bytes()).hexdigest()
    assert f"md:{want}" in lines[-1]
