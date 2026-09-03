"""L14 Task 7 — ledger changes: migration 7, variable stake, WHALE eligibility
branch, exit pnl in metrics (spec 2026-09-03 whale-consensus-lane-design.md §11)."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import schema
from arb.config import load
from arb.eligibility import classify
from arb.metrics import bet_confirmed, bet_pnl_optimistic
from arb.store import Store
from arb.tracker import process_poll

from tests.test_tracker import VENUE_OK, arb_opp

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
WHALE_VENUE_OK = {"polymarket": {"ok": True, "n": 1, "latency_ms": 1.0}}


@pytest.fixture
def cfg():
    c = load(ROOT / "config.yaml")
    c["ev_enabled"] = True
    return c


# --- migration 7 -------------------------------------------------------

def test_fresh_db_carries_variant_and_signal_id_at_schema_7(tmp_path):
    store = Store(tmp_path / "fresh.sqlite")
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(flat_bets)")}
    assert {"variant", "signal_id"} <= cols
    version = store.conn.execute(
        "SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == schema.SCHEMA_VERSION == 7


def test_migration_7_adds_columns_to_a_v6_db(tmp_path):
    # Genuine v6 flat_bets: strip the v7 columns from the CREATE TABLE that
    # schema.TABLES carries at HEAD, so the ALTERs in migrations.apply are
    # actually exercised instead of skipped by the PRAGMA table_info guard.
    v6_tables = [
        ddl.replace(", variant TEXT, signal_id INTEGER", "")
        if "CREATE TABLE IF NOT EXISTS flat_bets" in ddl else ddl
        for ddl in schema.TABLES
    ]
    path = tmp_path / "old_v6.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    for ddl in v6_tables + schema.INDEXES:
        con.execute(ddl)
    for v in range(1, 7):  # a real v6 DB accrued one row per migration, 1..6
        con.execute("INSERT INTO schema_version VALUES (?, datetime('now'))", (v,))
    con.commit()
    cols = {r[1] for r in con.execute("PRAGMA table_info(flat_bets)")}
    assert "variant" not in cols
    assert "signal_id" not in cols
    con.close()

    store = Store(path)  # __init__ runs migrations.apply -> must add columns, not error
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(flat_bets)")}
    assert {"variant", "signal_id"} <= cols
    version = store.conn.execute(
        "SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 7


def test_migration_7_is_idempotent_when_columns_already_present(tmp_path):
    # Simulates the "fresh DB already carries the column via TABLES" case that
    # every prior migration (gamepk/payout) guards against with PRAGMA table_info.
    path = tmp_path / "already_has_cols.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    for ddl in schema.TABLES + schema.INDEXES:  # TABLES already has variant/signal_id
        con.execute(ddl)
    con.execute("INSERT INTO schema_version VALUES (6, datetime('now'))")
    con.commit()
    from arb import migrations
    migrations.apply(con)  # must not raise "duplicate column name"
    cols = {r[1] for r in con.execute("PRAGMA table_info(flat_bets)")}
    assert {"variant", "signal_id"} <= cols
    con.close()


# --- _book: variable stake, variant, signal_id --------------------------

def _whale_cand(*, stake_usd=None, variant=None, signal_id=None, fp="fp-whale-1"):
    start = NOW + timedelta(hours=3)
    leg = {"venue": "polymarket", "book": "polymarket", "market_type": "WINNER",
           "line": None, "outcome": "Rays", "side": "YES", "venue_side": "YES",
           "cost": "0.55", "fee_cost": "0.01", "depth_usd": "50",
           "mid0": "0.54", "venue_market_id": "cond1:tok1", "fetched_at": NOW.isoformat()}
    cand = {
        "kind": "WHALE", "category": "sports", "sport": "baseball_mlb",
        "event_key": "pm:evt-1", "event_start_utc": start, "start_grade": "verified",
        "gamepk": None, "venue_pair": "polymarket:self", "fingerprint": fp,
        "position_key": f"pk-{fp}", "legs": [leg], "edge_points": "0.05",
        "cost_sum": Decimal("0.56"), "payout": Decimal(1), "roi": None,
        "status": "OK", "notes": "whale-consensus", "quality_flag": "none",
        "leg_skew_secs": 0.0, "depth_ok": False, "proof_status": None, "live": False,
    }
    if stake_usd is not None:
        cand["stake_usd"] = stake_usd
    if variant is not None:
        cand["variant"] = variant
    if signal_id is not None:
        cand["signal_id"] = signal_id
    return cand


def test_book_writes_variable_stake_variant_and_signal_id(cfg, tmp_path):
    store = Store(tmp_path / "book.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "whale",
                                "n_quotes": 1, "n_opps": 1})
    cand = _whale_cand(stake_usd=Decimal("0.42"), variant="V2/conf", signal_id=7)
    process_poll(cfg, store, p1, "whale", [cand], WHALE_VENUE_OK, now=NOW)
    bet = dict(store.query("SELECT * FROM flat_bets ORDER BY id DESC LIMIT 1")[0])
    assert bet["stake_usd"] == "0.420000"
    assert bet["variant"] == "V2/conf"
    assert bet["signal_id"] == 7
    assert bet["lane"] == "review"
    assert bet["notes"].startswith("whale-signal")


def test_book_defaults_stake_to_one_dollar_and_leaves_variant_signal_id_null(cfg, tmp_path):
    # existing lanes (ARB via candidate_from_opp) never set stake_usd/variant/
    # signal_id — must default exactly as before, proving no regression.
    store = Store(tmp_path / "book_default.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p1, "watch", [arb_opp(cfg, "0.47", "0.49")], VENUE_OK, now=NOW)
    bet = dict(store.query("SELECT * FROM flat_bets ORDER BY id DESC LIMIT 1")[0])
    assert bet["stake_usd"] == "1.000000"
    assert bet["variant"] is None
    assert bet["signal_id"] is None


def test_book_whale_candidate_without_stake_defaults_too(cfg, tmp_path):
    store = Store(tmp_path / "book_whale_default.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "whale",
                                "n_quotes": 1, "n_opps": 1})
    process_poll(cfg, store, p1, "whale", [_whale_cand(fp="fp-whale-2")],
                 WHALE_VENUE_OK, now=NOW)
    bet = dict(store.query("SELECT * FROM flat_bets ORDER BY id DESC LIMIT 1")[0])
    assert bet["stake_usd"] == "1.000000"
    assert bet["variant"] is None
    assert bet["signal_id"] is None


# --- classify(): WHALE is review-by-construction, first after ai_matched ----

def test_classify_whale_is_review_before_depth_or_any_other_check(cfg):
    # depth_ok False, quality_flag bad, start_grade absent -- none of it matters;
    # WHALE must short-circuit before any of those gates are even read.
    result = classify({"kind": "WHALE", "category": "sports", "live": False,
                       "depth_ok": False, "quality_flag": "bad_book",
                       "start_grade": "absent"}, cfg)
    assert result == (False, "whale-signal", "review")


def test_classify_whale_still_defers_to_ai_matched_check(cfg):
    # the WHALE branch sits AFTER ai_matched, not before (brief: "immediately
    # after the ai_matched check").
    result = classify({"kind": "WHALE", "ai_matched": True}, cfg)
    assert result == (False, "ai-matched", "review")


# --- metrics: exit pnl -------------------------------------------------

def _whale_bet(store, *, stake="1"):
    return store.insert("flat_bets", {
        "created_at": NOW.isoformat(), "poll_id": 1, "poll_kind": "whale",
        "fingerprint": "fp-exit-1", "position_key": "pk-exit-1", "kind": "WHALE",
        "category": "sports", "sport": "baseball_mlb", "venue_pair": "polymarket:self",
        "event_key": "pm:evt-1", "event_start_utc": NOW.isoformat(),
        "start_grade": "verified",
        "legs_json": json.dumps([{"venue": "polymarket", "market_type": "WINNER",
                                  "line": None, "outcome": "Rays", "side": "YES",
                                  "cost": "0.55", "fee_cost": "0.01",
                                  "venue_market_id": "cond1:tok1"}]),
        "stake_usd": stake, "edge_points": "0.05", "trusted": 0, "lane": "review",
        "quality_flag": "none", "leg_skew_secs": 0.0, "status": "OPEN",
        "pnl_optimistic": None, "variant": "V2/flat", "signal_id": 1})


def test_bet_pnl_optimistic_returns_exit_pnl_for_exit_sourced_settled_event(cfg, tmp_path):
    store = Store(tmp_path / "m1.sqlite")
    bet_id = _whale_bet(store)
    # inserted directly, never via settle_bet (spec §9) -- no per_leg key
    store.insert("bet_events", {
        "bet_id": bet_id, "created_at": NOW.isoformat(), "kind": "settled",
        "payload_json": json.dumps({"result": "EXITED", "source": "exit:flip",
                                    "exit_price": "0.40", "pnl": "-0.123456"})})
    bet = dict(store.query("SELECT * FROM flat_bets WHERE id=?", (bet_id,))[0])
    # without the fix, results=["EXITED"] hits the WON/LOST/VOID guard -> None
    assert bet_pnl_optimistic(store, bet) == Decimal("-0.123456")


def test_bet_confirmed_returns_exit_pnl_for_an_exited_bet(cfg, tmp_path):
    store = Store(tmp_path / "m2.sqlite")
    bet_id = _whale_bet(store)
    store.insert("bet_events", {
        "bet_id": bet_id, "created_at": NOW.isoformat(), "kind": "confirmed",
        "payload_json": json.dumps({"poll_id": 1, "cost_sum": "0.56", "legs": []})})
    store.insert("bet_events", {
        "bet_id": bet_id, "created_at": NOW.isoformat(), "kind": "settled",
        "payload_json": json.dumps({"result": "EXITED", "source": "exit:left",
                                    "exit_price": "0.30", "pnl": "-0.5"})})
    bet = dict(store.query("SELECT * FROM flat_bets WHERE id=?", (bet_id,))[0])
    assert bet_confirmed(store, bet) == (True, Decimal("-0.500000"))
