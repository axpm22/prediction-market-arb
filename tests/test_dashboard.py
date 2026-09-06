"""Dashboard (§14.3): read-only, serves the page + JSON state, 404s the rest."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from arb.config import load
from arb.dashboard import make_handler
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def server(tmp_path):
    cfg = load(ROOT / "config.yaml")
    Store(tmp_path / "t.sqlite")  # create schema
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, tmp_path / "t.sqlite"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_page_and_state(server):
    html = urllib.request.urlopen(f"{server}/").read().decode()
    assert "instrument panel" in html.lower()
    assert "gross of carry" in html
    state = json.loads(urllib.request.urlopen(f"{server}/api/state").read())
    assert state["supervisor"] in ("ALIVE", "STALE", "DEAD")
    assert state["headline_pnl"] == "0"
    assert state["ev_enabled"] is False
    assert state["trusted_pairs"] == []


def test_the_bootstrap_refresh_uses_the_configured_interval(tmp_path):
    """scheduleRefresh(timerSecs || 60) used to hard-code the bootstrap tick
    (the one that fires before /api/state has answered even once) regardless
    of dashboard.refresh_secs; the server now substitutes the config value in."""
    cfg = load(ROOT / "config.yaml")
    cfg["dashboard"]["refresh_secs"] = 45
    Store(tmp_path / "t.sqlite")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, tmp_path / "t.sqlite"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        html = urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}/").read().decode()
        assert "{{REFRESH_SECS}}" not in html
        assert "timerSecs || 45" in html
    finally:
        httpd.shutdown()


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{server}/etc/passwd")
    assert e.value.code == 404


def test_state_lanes_errors_schedule(tmp_path, monkeypatch):
    # UI pass 2026-07-18: per-lane timing, error feed, daily schedule
    import os
    from datetime import datetime, timezone

    from arb.dashboard import state
    cfg = load(ROOT / "config.yaml")
    store = Store(tmp_path / "s.sqlite")
    now = datetime.now(timezone.utc).isoformat()
    store.insert("polls", {"started_at": now, "kind": "watch",
                           "n_quotes": 7, "n_opps": 0})
    logdir = Path(os.environ["ARB_LOG_DIR"]) / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / "scan.log").write_text(
        "2026-07-18 00:01:02,003 WARNING arb.venues clob.polymarket.com: "
        "non-retryable TLS failure\n"
        "2026-07-18 00:01:03,000 INFO arb.venues fine\n")
    s = state(cfg, store)
    watch = [ln for ln in s["lanes"] if ln["name"] == "watch"][0]
    assert watch["interval_secs"] == float(cfg["watch_interval_secs"])
    assert watch["last_poll"] == now and watch["quotes_today"] == 7
    assert {ln["name"] for ln in s["lanes"]} >= \
        {k for k, v in cfg["supervisor"]["processes"].items() if v}
    errs = s["errors"]
    assert any("non-retryable TLS failure" in e["msg"] for e in errs)
    assert all(e["level"] != "INFO" for e in errs)
    keys = {sl["key"] for sl in s["schedule"]}
    assert {"settle", "report", "backup"} <= keys
    assert all(0 <= sl["due_in_secs"] <= 86400 for sl in s["schedule"])


# ====================== Task 12: P&L, positions, lanes, whale ================
# Fixtures come from Tasks 8/9 (tests/test_whale_lane.py, tests/test_whale_report.py):
# one tmp ledger + one tmp tape, run through a real `whale --once` cycle wherever
# the linkage (signal_id, whale list, quotes) has to be the lane's own.
import copy
import re
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from arb import bench, dashboard_data, dashboard_whale, report_whale, schema
from arb.report import daily_frame

from tests.test_whale_lane import CID, NOW, _ledger, _run, _tape
from tests.test_whale_report import _legacy, _whale_row


def _cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


def _today(cfg, now):
    return now.astimezone(ZoneInfo(str(cfg["timezone"]))).date().isoformat()


def _get(base, path):
    return json.loads(urllib.request.urlopen(f"{base}{path}").read())


@pytest.fixture
def whale_cfg(tmp_path):
    """The Task 8 lane config (see that module's docstring) with the tape
    pointed at this test's tmp file, which is what the dashboard opens ro."""
    from tests.test_whale_lane import cfg as _lane_cfg
    c = copy.deepcopy(load(ROOT / "config.yaml"))
    c["whale"]["min_score_lb"] = Decimal("-0.10")
    c["whale"]["exit"]["min_score_lb"] = Decimal("-0.20")
    c["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    assert _lane_cfg is not None                    # same knobs, one docstring
    return c


@pytest.fixture(autouse=True)
def _clear_whale_bundle_cache():
    """arb/dashboard_cache.py's memo is a process-global dict; the shared
    whale bundle (arb/dashboard_whale_bundle.py) keys it off cheap MAX() id
    probes, which two different tests' tmp fixtures can coincidentally share
    (e.g. two fresh, empty tapes both probe to (None, None, None)) — without
    this, a later test could read an earlier test's stale cached bundle."""
    from arb import dashboard_cache
    dashboard_cache._entries.clear()
    yield


# ---------------------------------------------------------------- /api/pnl

def test_pnl_series_end_where_daily_frame_says_they_end(tmp_path):
    """Two classes settled today: each series' last cumulative value IS
    report.daily_frame's settled_pnl for that class — one number, one source.

    The equality holds because these bets are booked AND settled inside the same
    window: `daily_frame` aggregates by booking day, the curve by settlement day,
    so a bet booked before the window and settled in it appears only on the
    curve (which is the point of the curve — see the next test)."""
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    store = Store(tmp_path / "p.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a", pnl="0.20", created=now)
    _whale_row(store, "V2/conf", 1, "pm:b", pnl="-0.10", created=now, stake="0.5")
    # invariant 5: an annotated-excluded row enters neither cell
    excluded = _whale_row(store, "V2/flat", 2, "pm:c", pnl="9.99", created=now)
    store.insert("annotations", {"created_at": now.isoformat(), "target_kind": "bet",
                                 "target_id": str(excluded),
                                 "kind": "settle-tripwire", "detail": "instant settle"})

    frame = daily_frame(cfg, store, _today(cfg, now))
    out = dashboard_data.pnl_series(cfg, store, 30)

    series = {(s["kind"], s["lane"], s["variant"]): s for s in out["series"]}
    assert set(series) == set(frame["by_class"])
    assert len(series) == 2
    for key, agg in frame["by_class"].items():
        assert Decimal(series[key]["values"][-1]) == agg["settled_pnl"]
    assert len(out["days"]) == 30
    assert out["days"][-1] == _today(cfg, now)


def test_a_curve_moves_on_the_day_a_bet_SETTLED_not_the_day_it_was_booked(tmp_path):
    """A bet booked ten days ago and settled today moves today's point — that is
    what a settled-P&L curve means."""
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    store = Store(tmp_path / "p.sqlite")
    bet_id = _whale_row(store, "V2/flat", 1, "pm:a", created=now - timedelta(days=10))
    store.insert("bet_events", {"bet_id": bet_id, "created_at": now.isoformat(),
                                "kind": "settled",
                                "payload_json": json.dumps({"result": "EXITED",
                                                            "source": "exit:flip",
                                                            "pnl": "0.30"})})

    (series,) = dashboard_data.pnl_series(cfg, store, 30)["series"]

    assert Decimal(series["values"][-2]) == 0
    assert Decimal(series["values"][-1]) == Decimal("0.30")


def test_pnl_bases_carry_the_three_bases_for_the_headline_class(tmp_path):
    """optimistic / confirmed / markout, side by side, headline class only."""
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    store = Store(tmp_path / "p.sqlite")
    bet_id = _legacy(store, created=now)
    store.insert("bet_events", {"bet_id": bet_id, "created_at": now.isoformat(),
                                "kind": "settled",
                                "payload_json": json.dumps({"result": "WON"})})

    out = dashboard_data.pnl_series(cfg, store, 7)

    bases = {b["basis"]: b for b in out["bases"]}
    assert set(bases) == {"optimistic", "confirmed", "markout"}
    # WON at cost 0.51: optimistic = stake*(1-c)/c; confirmed = $0 (never confirmed)
    assert Decimal(bases["optimistic"]["values"][-1]) > 0
    assert Decimal(bases["confirmed"]["values"][-1]) == 0


def test_api_pnl_honors_days_and_serves_json(tmp_path):
    cfg = _cfg()
    Store(tmp_path / "t.sqlite")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, tmp_path / "t.sqlite"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        assert len(_get(base, "/api/pnl?days=3")["days"]) == 3
        assert len(_get(base, "/api/pnl")["days"]) == int(cfg["dashboard"]["pnl_days"])
    finally:
        httpd.shutdown()


# ------------------------------------------------------------ /api/positions

def test_positions_price_an_open_whale_bet_at_bid_minus_fee(whale_cfg, tmp_path):
    """The open WHALE bet carries its signal's whales, its live exit status and
    an unrealized P&L marked at the latest quote's bid minus the taker fee."""
    from arb.models import q6
    from arb.venues.fees import leg_fee_cost
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(whale_cfg, store, con)

    rows = dashboard_data.positions(whale_cfg, store, now=NOW, con=con)

    flat = [r for r in rows if r["variant"] == "V1/flat"][0]
    assert flat["kind"] == "WHALE" and flat["lane"] == "review"
    assert flat["sport"] == "baseball_mlb" and flat["outcome"] == "Rays"
    assert Decimal(flat["bid"]) == Decimal("0.49")
    stake, cost = Decimal(flat["stake_usd"]), Decimal(flat["entry_cost"])
    fee = leg_fee_cost(whale_cfg, "polymarket", "sports", Decimal("0.49"))
    assert Decimal(flat["unrealized_pnl"]) == q6(
        (Decimal("0.49") - fee) * (stake / cost) - stake)
    assert flat["signal_id"] is not None
    whales = flat["whale"]["whales"]
    assert {w["wallet"] for w in whales} == {"w0", "w1", "w2"}
    assert all(w["lb"] is not None and w["native"] is not None for w in whales)
    assert "exit_reason" in flat["whale"]        # None while the signal holds


def test_positions_report_no_quote_rather_than_a_wrong_mark(tmp_path):
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    store = Store(tmp_path / "p.sqlite")
    _legacy(store, created=now)                  # no quotes row for its leg

    (row,) = dashboard_data.positions(cfg, store, now=now)

    assert row["bid"] is None and row["unrealized_pnl"] is None
    assert row["whale"] is None                  # not a WHALE row
    assert row["age_secs"] >= 0


def test_positions_exclude_settled_void_and_lifetime_closed(tmp_path):
    """`lifetime_closed` is terminal only for a LOCKED kind (here ARB): those
    are settled off the flag itself, so losing the flag ends the position. A
    resolution-settled kind is settled off an oracle and is still held — see
    the next test (§9, final review I3)."""
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    store = Store(tmp_path / "p.sqlite")
    for kind, bet_kind in (("settled", "EV"), ("void", "EV"),
                           ("lifetime_closed", "ARB")):
        bet_id = _legacy(store, kind=bet_kind, fp=f"fp-{kind}", created=now)
        store.insert("bet_events", {"bet_id": bet_id, "created_at": now.isoformat(),
                                    "kind": kind, "payload_json": "{}"})
    _legacy(store, fp="fp-open", created=now)

    rows = dashboard_data.positions(cfg, store, now=now)

    assert [r["fingerprint"] for r in rows] == ["fp-open"]


def test_a_lifetime_closed_resolution_settled_bet_is_still_an_open_position(
        whale_cfg, tmp_path):
    """I3: §9 pins ONE definition of open — no `settled`/`void` event — and the
    lane keeps exiting a `lifetime_closed` WHALE bet. The panel called it gone,
    so the panel and the lane disagreed about what we hold."""
    from arb import dashboard
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(whale_cfg, store, con)
    arb_id = _legacy(store, kind="ARB", fp="fp-arb", created=NOW)
    for bet_id in [b["id"] for b in store.query("SELECT id FROM flat_bets")]:
        store.insert("bet_events", {"bet_id": bet_id, "created_at": NOW.isoformat(),
                                    "kind": "lifetime_closed", "payload_json": "{}"})

    rows = dashboard_data.positions(whale_cfg, store, now=NOW, con=con)

    assert {r["kind"] for r in rows} == {"WHALE"}          # the ARB row is gone
    assert arb_id not in {r["id"] for r in rows}
    assert {b["kind"] for b in dashboard.state(whale_cfg, store)["open_bets"]} \
        == {"WHALE"}


# ---------------------------------------------------------------- /api/lanes

def test_lane_verdicts_take_the_latest_row_per_lane_and_flag_stale(tmp_path):
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    store = Store(tmp_path / "b.sqlite")
    old = (now - timedelta(days=int(cfg["dashboard"]["bench_stale_days"]) + 1)).isoformat()
    for ran_at, verdict in ((old, "UNDERPOWERED"), (now.isoformat(), "SIGNAL")):
        store.insert("bench_results", {"ran_at": ran_at, "lane": "L8",
                                       "primary_value": "0.043", "ci_lo": "0.01",
                                       "ci_hi": "0.08", "n": 700, "verdict": verdict,
                                       "payload_json": "{}"})
    store.insert("bench_results", {"ran_at": old, "lane": "L14",
                                   "primary_value": None, "ci_lo": None, "ci_hi": None,
                                   "n": 0, "verdict": "BLOCKED", "payload_json": "{}"})

    rows = {r["lane"]: r for r in dashboard_data.lane_verdicts(cfg, store, now=now)}

    assert len(rows) == 2
    assert rows["L8"]["verdict"] == "SIGNAL" and rows["L8"]["stale"] is False
    assert rows["L14"]["stale"] is True


def test_bench_persists_one_row_per_lane_result(tmp_path):
    store = Store(tmp_path / "b.sqlite")
    results = [bench.LaneResult(lane="L1", name="a", primary="p", n=5,
                                estimate=Decimal("0.1"), lo=Decimal("0"),
                                hi=Decimal("0.2"), verdict="UNDERPOWERED"),
               bench.LaneResult(lane="L14", name="b", primary="q", note="no sample")]

    bench.persist(store, results, "2026-09-04T00:00:00+00:00")

    rows = [dict(r) for r in store.query("SELECT * FROM bench_results ORDER BY id")]
    assert [r["lane"] for r in rows] == ["L1", "L14"]
    assert rows[0]["primary_value"] == "0.1" and rows[0]["n"] == 5
    assert rows[1]["primary_value"] is None and rows[1]["verdict"] == "BLOCKED"
    assert json.loads(rows[1]["payload_json"])["note"] == "no sample"


def test_cmd_bench_appends_its_results(tmp_path, monkeypatch, capsys):
    """The append happens where the results are rendered, once per run."""
    import argparse

    from arb import commands
    cfg = _cfg()
    cfg["bench"]["db"] = str(tmp_path / "hist.sqlite")     # absent -> BLOCKED lanes
    store = Store(tmp_path / "b.sqlite")
    args = argparse.Namespace(pull=False, probe_anj=False, only="L14")

    assert commands.cmd_bench(cfg, store, args) == 0

    rows = store.query("SELECT * FROM bench_results")
    assert len(rows) == 1 and rows[0]["lane"] == "L14"


# --------------------------------------------------------------- migration 8

def test_fresh_db_is_at_schema_8_with_bench_results(tmp_path):
    store = Store(tmp_path / "fresh.sqlite")
    assert schema.SCHEMA_VERSION >= 8            # HEAD, whatever it is now (v9 added child_exits)
    assert store.conn.execute(
        "SELECT MAX(version) FROM schema_version").fetchone()[0] == schema.SCHEMA_VERSION
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(bench_results)")}
    assert {"id", "ran_at", "lane", "primary_value", "ci_lo", "ci_hi", "n",
            "verdict", "payload_json"} == cols


def test_migration_8_is_idempotent_on_a_v7_db(tmp_path):
    import sqlite3

    from arb import migrations
    path = tmp_path / "v7.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    for ddl in schema.TABLES + schema.INDEXES:
        con.execute(ddl)
    for v in range(1, 8):
        con.execute("INSERT INTO schema_version VALUES (?, datetime('now'))", (v,))
    con.commit()
    migrations.apply(con)
    migrations.apply(con)               # re-open re-runs: must not raise
    # applying from a v7 db runs every later migration in the same pass, not
    # just 8 — HEAD, whatever it is now (v9 added child_exits)
    assert con.execute(
        "SELECT MAX(version) FROM schema_version").fetchone()[0] == schema.SCHEMA_VERSION
    con.close()


# --------------------------------------------------------------- migration 9

def test_fresh_db_is_at_head_schema_with_child_exits(tmp_path):
    store = Store(tmp_path / "fresh.sqlite")
    assert schema.SCHEMA_VERSION == 9
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(child_exits)")}
    assert {"id", "process", "exited_at", "rc", "runtime_secs", "killed"} == cols


def test_migration_9_is_idempotent_on_a_v8_db(tmp_path):
    import sqlite3

    from arb import migrations
    path = tmp_path / "v8.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
    for ddl in schema.TABLES + schema.INDEXES:
        con.execute(ddl)
    for v in range(1, 9):
        con.execute("INSERT INTO schema_version VALUES (?, datetime('now'))", (v,))
    con.commit()
    migrations.apply(con)
    migrations.apply(con)               # re-open re-runs: must not raise
    assert con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
    con.close()


# ---------------------------------------------------------------- /api/whale

def test_whale_panel_renders_on_an_empty_tape(whale_cfg, tmp_path):
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)

    panel = dashboard_whale.whale_panel(whale_cfg, store, now=NOW, con=con)

    assert panel["tape_ok"] is True
    assert panel["wallets"]["tracked"] == 0 and panel["roi"] == {}
    assert [r["rule"] for r in panel["kill"]["rules"]] == [1, 2, 3, 4]
    assert all(r["state"] in ("armed", "not yet", "FIRED")
               for r in panel["kill"]["rules"])


def test_whale_panel_on_the_task_8_fixture_tape(whale_cfg, tmp_path):
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(whale_cfg, store, con)

    panel = dashboard_whale.whale_panel(whale_cfg, store, now=NOW, con=con)

    # the fixture's prints are $500 notional, below discovery_min_notional_usd,
    # so no wallet is TRACKED — they are still scored and still qualify
    assert panel["wallets"]["tracked"] == 0
    assert panel["signals"]["evaluated_24h"] == 6    # V1, V2, V3, V4, V5, V6
    assert panel["collector"]["status"] == "ok" and panel["collector"]["rows"] == 3
    assert panel["qualifying"]["n"] == 3


def test_api_whale_and_positions_and_lanes_are_json_over_http(whale_cfg, tmp_path):
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(whale_cfg, store, con)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                make_handler(whale_cfg, store.path))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        whale = _get(base, "/api/whale")["whale"]
        assert whale["tape_ok"] is True and whale["collector"]["rows"] == 3
        assert len(whale["kill"]["rules"]) == 4
        rows = _get(base, "/api/positions")["positions"]
        assert {r["variant"] for r in rows} == {"V1/flat", "V1/conf", "V1/kelly",
                                                "V6/flat", "V6/conf", "V6/kelly"}
        assert _get(base, "/api/lanes")["lanes"] == []
    finally:
        httpd.shutdown()


# ------------------------------------------------- shared whale-state bundle
# Live incident 2026-09-05 ~11:00Z: /api/whale, /api/whale/traders and
# /api/positions each ran their own score_state pass (~30s on the live
# 764k-print tape); polled every refresh, that was 3-4 simultaneous 30s CPU
# burns — 231% CPU, 12 open connections, /api/whale not answering inside
# 180s. arb/dashboard_whale_bundle.py fixes it: one build, shared.

def test_two_whale_endpoints_back_to_back_run_score_state_once(whale_cfg, tmp_path,
                                                                monkeypatch):
    import arb.dashboard_whale_bundle as bundle_mod
    from arb.dashboard import _section
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(whale_cfg, store, con)

    calls = []
    real = bundle_mod.score_state

    def counting(cfg, con, now):
        calls.append(1)
        return real(cfg, con, now)

    monkeypatch.setattr(bundle_mod, "score_state", counting)

    whale = _section(whale_cfg, store, "/api/whale", {})
    traders = _section(whale_cfg, store, "/api/whale/traders", {})
    positions = _section(whale_cfg, store, "/api/positions", {})

    assert len(calls) == 1                          # ONE pass shared by all three
    assert whale["whale"]["tape_ok"] is True
    assert traders["tape_ok"] is True
    assert {r["variant"] for r in positions["positions"]} == \
        {"V1/flat", "V1/conf", "V1/kelly", "V6/flat", "V6/conf", "V6/kelly"}


def test_a_new_print_invalidates_the_bundle(whale_cfg, tmp_path, monkeypatch):
    """The bundle is keyed off MAX(prints.id) among other cheap probes: a
    genuinely new print forces a fresh score_state pass instead of serving
    the stale one for the rest of cache_secs, but nothing new is still one
    cache hit — never a recompute per request."""
    import arb.dashboard_whale_bundle as bundle_mod
    from arb.dashboard import _section
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(whale_cfg, store, con)

    calls = []
    real = bundle_mod.score_state

    def counting(cfg, con, now):
        calls.append(1)
        return real(cfg, con, now)

    monkeypatch.setattr(bundle_mod, "score_state", counting)

    _section(whale_cfg, store, "/api/whale", {})
    assert len(calls) == 1
    _section(whale_cfg, store, "/api/whale", {})
    assert len(calls) == 1                          # unchanged probes: still a hit

    con.execute(
        "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, condition_id,"
        " asset, outcome_index, side, price, size, notional_usd, event_slug, source)"
        " VALUES (?,?,?,?,?,?,0,'BUY',?,?,?,?,'tape')",
        (NOW.isoformat(), int(NOW.timestamp()), "0xnewprint", "0xw9", CID, "tok",
         "0.500000", "1000.000000", "500.000000", "e"))
    con.commit()

    _section(whale_cfg, store, "/api/whale", {})

    assert len(calls) == 2                          # a new print forced a rebuild


def test_write_daily_never_touches_the_dashboard_cache(whale_cfg, tmp_path):
    """report.write_daily's whale section (report_whale.whale_frame) must stay
    a pure function of the tape/ledger, reproducible for a past day (rule 9)
    — it must never read or write arb/dashboard_cache.py's memo."""
    from arb import dashboard_cache, report
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(whale_cfg, store, con)

    report.write_daily(whale_cfg, store, _today(whale_cfg, NOW),
                       outdir=tmp_path / "reports")

    assert dashboard_cache._entries == {}


def test_positions_evaluate_exits_against_the_bundles_own_now_not_wall_clock(
        whale_cfg, tmp_path, monkeypatch):
    """Dashboard review of c83964f: `_section` used to hand `dd.positions` a
    FRESH `datetime.now()` while `b["state"]` can be up to cache_secs old —
    and that `now` feeds `whale_exit.exit_reason`'s start gate
    (arb/whale_exit.py ~54: `if start is None or ts(now) >= start: return
    None`). A request landing after a bet's start, served from a bundle that
    is still the pre-start cache entry (nothing on the tape changed, so
    `_probe_key` still hits), would silently force every WHALE bet's exit
    status to None for a reason that has nothing to do with the tape — purely
    because wall-clock time passed between polls. /api/positions must
    evaluate against the SAME `now` the served bundle was built with."""
    import arb.dashboard as dashboard_mod
    import arb.whale_exit as whale_exit_mod
    from arb.dashboard import _section
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(whale_cfg, store, con)                     # books WHALE bets, start_utc = START

    seen_now = []
    real_exit_reason = whale_exit_mod.exit_reason

    def spy(bet_ctx, open_positions_now, score_lookup, now, cfg, **kw):
        seen_now.append(now)
        return real_exit_reason(bet_ctx, open_positions_now, score_lookup, now, cfg, **kw)

    monkeypatch.setattr(whale_exit_mod, "exit_reason", spy)

    class FixedDatetime(datetime):
        _now = NOW

        @classmethod
        def now(cls, tz=None):
            return cls._now

    monkeypatch.setattr(dashboard_mod, "datetime", FixedDatetime)

    _section(whale_cfg, store, "/api/positions", {})       # builds+caches the bundle at NOW
    assert seen_now and seen_now[0] == NOW

    FixedDatetime._now = NOW + timedelta(hours=4)          # past START (=NOW+3h): the gate
    _section(whale_cfg, store, "/api/positions", {})       # would close on a fresh clock
                                                             # cache still hits: nothing on
                                                             # the tape changed
    assert seen_now[-1] == NOW                             # the bundle's own now, not the
                                                             # request's fresh wall clock


def test_api_whale_traders_ranks_every_wallet_over_http(whale_cfg, tmp_path):
    """`/api/whale/traders`: the read-time profiles, Decimals as strings. The
    `?wallet=` form is that one wallet's root -> sport -> bucket record plus
    the buckets it is actually good in."""
    from tests.test_whale_rank import _tape as _rank_tape
    store = Store(tmp_path / "t.sqlite")
    _rank_tape(tmp_path, whale_cfg)                 # 6 resolved positions, 5 won
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(whale_cfg, store.path))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        body = _get(base, "/api/whale/traders")
        assert body["tape_ok"] is True and body["level"] == "bucket"
        assert body["min_n"] == 5 and body["top"] == 15
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert (row["wallet"], row["key"], row["n"]) == \
            ("0xwhale", "basketball_nba:WINNER", 6)
        assert row["win_rate"] == "0.833333" and row["implied"] == "0.600000"
        assert row["native"] is True and row["hygiene_flag"] is False
        assert {"excess_win_rate", "edge_mean", "edge_sd", "roi_at_risk", "t_stat",
                "lb80", "excess_vs_base", "notional", "last_resolved_at"} <= set(row)

        one = _get(base, "/api/whale/traders?wallet=0xwhale")
        assert [r["level"] for r in one["rows"]] == ["root", "sport", "bucket"]
        assert one["best"][0]["key"] == "basketball_nba:WINNER"
        assert _get(base, "/api/whale/traders?level=root")["rows"][0]["key"] == "sports"
        assert _get(base, "/api/whale/traders?min_n=99")["rows"] == []
    finally:
        httpd.shutdown()


def test_the_traders_table_is_sortable_and_defaults_to_lb80():
    html = (ROOT / "arb" / "dashboard.html").read_text()
    script = html.split("<script>")[1]
    assert 'id="traders"' in html
    assert "'/api/whale/traders'" in script
    assert "tSort = 'lb80'" in script                # default sort column
    assert "sortTraders(" in script and "toggleTrader(" in script


def test_a_broken_section_reports_itself_instead_of_500ing(tmp_path, monkeypatch):
    """One failing section must not take the page down with it."""
    cfg = _cfg()
    Store(tmp_path / "t.sqlite")
    monkeypatch.setattr(dashboard_data, "lane_verdicts",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, tmp_path / "t.sqlite"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        body = _get(f"http://127.0.0.1:{httpd.server_port}", "/api/lanes")
        assert "boom" in body["error"]
    finally:
        httpd.shutdown()


# ------------------------------------------------------------------ the page

def test_the_script_references_every_element_id():
    html = (ROOT / "arb" / "dashboard.html").read_text()
    body, script = html.split("<script>")
    ids = set(re.findall(r'id="([^"]+)"', body))
    assert {"pnl", "bases", "positions", "bench", "whale", "kill"} <= ids
    for element_id in ids:
        assert f"'{element_id}'" in script, element_id


def test_the_refresh_timer_is_armed_before_the_page_awaits_anything():
    """A slow or failing /api/state must not freeze every section forever: the
    interval used to be set only after that await resolved."""
    script = (ROOT / "arb" / "dashboard.html").read_text().split("<script>")[1]
    body = script[script.index("async function refresh()"):]
    assert body.index("scheduleRefresh(") < body.index("await fetch")
    assert script.count("setInterval(") == 1        # only scheduleRefresh arms it


def test_a_broken_whale_section_says_so_in_the_kill_block_too():
    """#kill renders from the same /api/whale payload; blank reads as 'no rules'."""
    script = (ROOT / "arb" / "dashboard.html").read_text().split("<script>")[1]
    draw = script[script.index("function drawWhale("):]
    assert "section('kill', w)" in draw[:draw.index("$('whale').innerHTML")]


# -------------------------------------------------- dashboard.mode: whale

def test_whale_mode_leads_with_headline_then_roi_positions_traders_errors_strip(tmp_path):
    """Operator instruction 2026-09-05: the headline (P&L + general stats)
    strip leads, then the ROI/exits section, open positions, traders, errors,
    and the supervisor lamp strip last — supersedes 2026-09-04's whale1/
    whale2/positions/lamps/errsec order."""
    cfg = _cfg()
    cfg["dashboard"]["mode"] = "whale"
    Store(tmp_path / "t.sqlite")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, tmp_path / "t.sqlite"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/").read().decode()
        assert "<title>Whale lane — instrument panel</title>" in html
        i_header = html.index("</header>")
        i_headline = html.index('id="whale-headline"')
        i_whale1 = html.index("L14 whale-consensus lane")
        i_positions = html.index("Open positions — marked")
        i_whale2 = html.index("Traders — where each wallet is good")
        i_errsec = html.index('id="errsec"')
        i_lamps = html.index('id="lamps"')
        assert i_header < i_headline < i_whale1 < i_positions < i_whale2 \
            < i_errsec < i_lamps
        assert 'const MODE = "whale";' in html
    finally:
        httpd.shutdown()


def test_headline_strip_first_tile_is_pnl_in_whale_mode():
    """The headline strip's very first tile is P&L (operator 2026-09-05)."""
    html = (ROOT / "arb" / "dashboard.html").read_text()
    script = html.split("<script>")[1]
    assert '<section id="whale-headline" hidden>' in html
    draw = script[script.index("function drawHeadline("):
                  script.index("function drawWhale(")]
    v = draw.index("$('wh-tiles').innerHTML =")
    pnl_pos = draw.index("P&amp;L", v)
    next_tile_pos = draw.index("'open positions", v)
    assert v < pnl_pos < next_tile_pos


def test_headline_frame_carries_realized_pnl_dollars_not_per_dollar_roi(whale_cfg, tmp_path):
    """The dashboard headline's P&L tile needs true dollars: `roi_rows`
    divides pnl by stake for the per-root/variant CI tables, but the headline
    wants Σ pnl across every settled WHALE row — every sizing (flat/conf/
    kelly) is its own real paper stake, so all three count."""
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a", pnl="1.50", stake="1")
    _whale_row(store, "V2/conf", 1, "pm:a", pnl="3.75", stake="2.5")
    _whale_row(store, "V2/kelly", 1, "pm:a", pnl="-0.80", stake="4")

    frame = report_whale.whale_frame(whale_cfg, store, now=NOW)

    assert frame["pnl"] == {"realized": Decimal("4.45"), "realized_n": 3}


def test_headline_frame_pnl_is_zero_not_none_on_an_empty_ledger(whale_cfg, tmp_path):
    """Zero settled bets must read as $0, never crash / NaN: `realized_pnl`
    always returns a concrete Decimal, not None, on an empty ledger."""
    store = Store(tmp_path / "t.sqlite")

    frame = report_whale.whale_frame(whale_cfg, store, now=NOW)

    assert frame["pnl"] == {"realized": Decimal("0"), "realized_n": 0}


def test_full_mode_headline_strip_stays_hidden_and_unmoved(tmp_path):
    """Full mode must render unchanged: the new strip exists in the markup
    (mode is server-templated) but stays `hidden` and is never spliced to the
    top — whale_first() only runs for dashboard.mode: whale."""
    cfg = _cfg()
    cfg["dashboard"]["mode"] = "full"
    from arb.dashboard import render_page
    html = render_page(cfg).decode()
    assert '<section id="whale-headline" hidden>' in html
    # unmoved: still sits after the pnl/bench sections that keep their
    # original position ahead of it, exactly as before this change
    assert html.index('id="pnl"') < html.index('id="whale-headline"') \
        < html.index("L14 whale-consensus lane")


def test_whale_md_ignores_the_new_realized_pnl_frame_field(whale_cfg, tmp_path):
    """report_whale._frame_from_state now carries frame['pnl'] (realized $ for
    the dashboard headline strip) — whale_md must keep printing exactly what
    it printed before, so write_daily's hashed markdown (rule 9's chain) is
    unaffected by this change."""
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a", pnl="1.50", stake="1")

    frame = report_whale.whale_frame(whale_cfg, store, now=NOW)
    assert frame["pnl"]["realized"] == Decimal("1.50")   # the field really is there

    stripped = dict(frame)
    del stripped["pnl"]
    assert report_whale.whale_md(frame) == report_whale.whale_md(stripped)


def test_whale_md_ignores_the_exits_24h_frame_field(whale_cfg, tmp_path):
    """frame['exits_24h'] (headline strip's 24h exit count, arb/whale_pnl.py
    `exits_recent`) is a LIVE figure like `pnl` above — whale_md must keep
    printing exactly what it printed before this field existed, so
    write_daily's hashed markdown (rule 9's chain) is unaffected by it."""
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a", pnl="1.50", stake="1")

    frame = report_whale.whale_frame(whale_cfg, store, now=NOW)
    # _whale_row's settled event is always source "exit:flip" (test_whale_report.py)
    assert frame["exits_24h"] == {"flip": 1, "left": 0}   # the field really is there

    stripped = dict(frame)
    del stripped["exits_24h"]
    assert report_whale.whale_md(frame) == report_whale.whale_md(stripped)


def test_full_mode_keeps_todays_layout(tmp_path):
    cfg = _cfg()
    cfg["dashboard"]["mode"] = "full"
    Store(tmp_path / "t.sqlite")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, tmp_path / "t.sqlite"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/").read().decode()
        assert "<title>ArbitrageModel — instrument panel</title>" in html
        assert html.index('id="lamps"') < html.index("Open positions — marked")
        assert html.index("Open positions — marked") < html.index("L14 whale-consensus lane")
        assert 'const MODE = "full";' in html
    finally:
        httpd.shutdown()


def test_whale_panel_table_walks_roots_before_variant_or_reason():
    """ee8b37f (2026-09-04, non-sports roots): `/api/whale`'s roi/bets/exits
    are now {root: {variant_or_reason: cell}} — sports (primary_root) first,
    never pooled across roots. drawWhale must walk root then variant/reason;
    indexing straight off a variant/reason key (the pre-ee8b37f shape) reads
    every cell as undefined."""
    html = (ROOT / "arb" / "dashboard.html").read_text()
    script = html.split("<script>")[1]
    draw = script[script.index("function drawWhale("):script.index("function drawTraders(")]
    assert "f.primary_root" in draw
    assert "f.roi[f.primary_variant]" not in draw
    assert "f.roi[v]" not in draw and "f.bets[v]" not in draw


def test_whale_mode_js_hides_the_arb_only_pieces_and_filters_positions():
    """The pieces the server didn't move (tiles/gates/pnl/bench) are hidden by
    the page's own JS, guarded on the templated MODE constant — checked at the
    source level, same as this file's other script-logic tests."""
    html = (ROOT / "arb" / "dashboard.html").read_text()
    script = html.split("<script>")[1]
    assert "MODE === 'whale'" in script
    assert "$('tiles').hidden = true" in script
    assert "$('gates').hidden = true" in script
    assert "$('pnl').closest('section').hidden = true" in script
    assert "$('bench').closest('section').hidden = true" in script
    draw = script[script.index("function drawPositions("):]
    assert "b.kind === 'WHALE'" in draw[:draw.index("function ", 10)]


def test_tail_errors_whale_mode_drops_disabled_venues_and_lanes(tmp_path):
    """Fix pass on 5c9bbb5: the errors feed still showed sx/kalshi watch
    failures and disabled-lane warnings (negrisk etc.) in whale mode. Drop
    those; never drop arb.whale*/arb.supervisor/arb.oddsapi lines (rule 2:
    the low-credit WARNING must always survive — oddsapi can never be in
    watch_venues, so routing it through the venue check hid it forever), or
    an arb.watch line about an enabled venue (polymarket, per config.yaml
    watch_venues). arb.kalshi_events IS the negrisk lane (it's what
    nonsports/kalshi_events.py logs as), so it's dropped in whale mode too
    (negrisk is off in config.yaml's whale-mode processes block) — the
    generic `source.split(".",1)[1]` lane guess used to miss this because
    "kalshi_events" isn't a supervisor.processes key. arb.whale logs to
    whale.log, not scan.log (arb/whale_tape.py etc.) — exercised here so a
    regression on _tail_errors' log-file list regresses this test too."""
    import os

    from arb.dashboard import _tail_errors
    logdir = Path(os.environ["ARB_LOG_DIR"]) / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / "scan.log").write_text(
        "2026-09-05 00:01:00,000 WARNING arb.watch venue fetch failed: sx: 400 from https://x\n"
        "2026-09-05 00:01:01,000 WARNING arb.negrisk something broke\n"
        "2026-09-05 00:01:03,000 WARNING arb.watch venue fetch failed: polymarket: 500 from https://y\n"
        "2026-09-05 00:01:04,000 WARNING arb.supervisor child restart\n"
        "2026-09-05 00:01:05,000 WARNING arb.kalshi pagination cap hit\n"
        "2026-09-05 00:01:06,000 WARNING arb.oddsapi x-requests-remaining: 42, low\n"
        "2026-09-05 00:01:07,000 WARNING arb.kalshi_events something broke\n")
    (logdir / "whale.log").write_text(
        "2026-09-05 00:01:02,000 WARNING arb.whale wallet backfill slow\n")

    cfg = _cfg()
    cfg["dashboard"]["mode"] = "whale"
    assert cfg["scan_times"] == []                 # rule-2 warning must survive even so
    whale = _tail_errors(cfg)
    cfg_full = copy.deepcopy(cfg)
    cfg_full["dashboard"]["mode"] = "full"
    full = _tail_errors(cfg_full)

    assert {e["source"] for e in whale} == \
        {"arb.watch", "arb.whale", "arb.supervisor", "arb.oddsapi"}
    assert {e["msg"] for e in whale if e["source"] == "arb.watch"} == \
        {"venue fetch failed: polymarket: 500 from https://y"}
    assert {e["source"] for e in full} == \
        {"arb.watch", "arb.negrisk", "arb.whale", "arb.supervisor", "arb.kalshi",
         "arb.oddsapi", "arb.kalshi_events"}


def test_tail_errors_picks_up_tracebacks_from_child_logs(tmp_path, monkeypatch):
    """2026-09-05 incident: a crashing child's traceback went to the
    supervisor's own stdout, which the errors panel never looked at — it
    must also tail supervisor.child_log_dir's raw per-process capture
    files (arb/heartbeat.py, arb/child_log.py) for Traceback/Error lines."""
    monkeypatch.setenv("ARB_LOG_DIR", str(tmp_path))  # isolate from other tests' child logs

    from arb.dashboard import _tail_errors
    cfg = _cfg()
    d = tmp_path / cfg["supervisor"]["child_log_dir"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "whale-backfill.log").write_text(
        "2026-09-05 12:00:00,000 plain child output, nothing wrong\n"
        "Traceback (most recent call last):\n"
        "  File \"whale_backfill.py\", line 12, in <module>\n"
        "ValueError: boom\n")
    errors = _tail_errors(cfg)
    msgs = {e["msg"] for e in errors if e["source"] == "child:whale-backfill"}
    assert "Traceback (most recent call last):" in msgs
    assert "ValueError: boom" in msgs
    assert "plain child output, nothing wrong" not in msgs


def test_unknown_logger_is_never_hidden_in_whale_mode():
    """dashboard_mode can't classify every logger name into a
    supervisor.processes lane; when it can't, keep the line rather than
    guess — a hidden real error is worse than a noisy unclassified one."""
    from arb.dashboard_mode import _keep_error_line
    cfg = _cfg()
    cfg["dashboard"]["mode"] = "whale"
    assert _keep_error_line(cfg, "some.other.logger", "whatever") is True


def test_disabled_lane_child_traceback_is_hidden_in_whale_mode():
    """I2 (review of 9cb356b): tail_child_errors synthesises sources
    child:<name>, which _keep_error_line used to fail to classify (fell
    through to the "unclassified -> keep" default) — so a disabled lane's
    child traceback was never filtered in whale mode. child:<name> must map
    to lane <name> (the capture filename IS the supervisor.processes key)
    and obey the same disabled-lane rule; whale/whale-backfill/watch are core
    to whale mode and stay visible even when switched off."""
    from arb.dashboard_mode import _keep_error_line
    cfg = _cfg()
    cfg["dashboard"]["mode"] = "whale"
    cfg["supervisor"]["processes"] = {**cfg["supervisor"]["processes"],
                                      "negrisk": False, "watch": False, "whale": False}
    assert _keep_error_line(cfg, "child:negrisk", "Traceback...") is False
    assert _keep_error_line(cfg, "child:whale-backfill", "Traceback...") is True
    assert _keep_error_line(cfg, "child:whale", "Traceback...") is True
    assert _keep_error_line(cfg, "child:watch", "Traceback...") is True
    # a clock command (e.g. settle) is never a processes key, so never "disabled"
    assert _keep_error_line(cfg, "child:settle", "Traceback...") is True


def test_whale_core_lanes_constant_matches_keep_error_line_exemptions():
    """M6 (review of 798aae6): _keep_error_line's inline
    ("watch","whale","whale-backfill") tuple is a named _WHALE_CORE constant
    beside _LANE_LOGGERS, so the "core to whale mode" set has one home."""
    from arb.dashboard_mode import _WHALE_CORE
    assert set(_WHALE_CORE) == {"watch", "whale", "whale-backfill"}


def test_tail_child_errors_reads_newest_dated_file_per_name(tmp_path, monkeypatch):
    """M8: an undated logs/children/<name>.log grew forever with nothing to
    rotate it (log_retention_days' purge only reclaims a file whose mtime
    stopped moving, which an ACTIVE file's never does). Filenames are now
    date-stamped (<name>.<YYYY-MM-DD>.log); tail_child_errors must read only
    the NEWEST file per child name, not every dated file still inside
    retention."""
    monkeypatch.setenv("ARB_LOG_DIR", str(tmp_path))
    from arb.dashboard_mode import tail_child_errors
    cfg = _cfg()
    d = tmp_path / cfg["supervisor"]["child_log_dir"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "whale-backfill.2026-09-03.log").write_text(
        "Traceback (most recent call last):\nValueError: old day\n")
    (d / "whale-backfill.2026-09-05.log").write_text(
        "Traceback (most recent call last):\nValueError: new day\n")

    errors = tail_child_errors(cfg)

    msgs = {e["msg"] for e in errors if e["source"] == "child:whale-backfill"}
    assert "ValueError: new day" in msgs
    assert "ValueError: old day" not in msgs


def test_tail_child_errors_ignores_a_stale_legacy_undated_file(tmp_path, monkeypatch):
    """CRITICAL, review of 70058b9: grouping-by-filename-stem put the legacy
    undated <name>.log (pre-M8) in the SAME group as <name>.<date>.log and
    picked the newest by comparing p.name lexically — "watch.log" >
    "watch.2026-09-05.log" as strings (the digit '2' sorts before the letter
    'l'), so the legacy file always won regardless of which is actually
    newer. On the live box logs/children/{watch,whale,whale-backfill}.log
    predate this change, so after a supervisor restart the errors panel
    would freeze on stale pre-migration content for up to
    log_retention_days. Must pick the newest per name by mtime instead."""
    import os
    import time as time_mod

    monkeypatch.setenv("ARB_LOG_DIR", str(tmp_path))
    from arb.dashboard_mode import tail_child_errors
    cfg = _cfg()
    d = tmp_path / cfg["supervisor"]["child_log_dir"]
    d.mkdir(parents=True, exist_ok=True)
    legacy = d / "watch.log"
    legacy.write_text("Traceback (most recent call last):\nValueError: stale legacy\n")
    dated = d / "watch.2026-09-05.log"
    dated.write_text("Traceback (most recent call last):\nValueError: fresh dated\n")
    old_time = time_mod.time() - 3600
    os.utime(legacy, (old_time, old_time))  # legacy is OLDER by mtime ...

    errors = tail_child_errors(cfg)

    # ... but "watch.log" > "watch.2026-09-05.log" lexically, the bug this guards
    msgs = {e["msg"] for e in errors if e["source"] == "child:watch"}
    assert "ValueError: fresh dated" in msgs
    assert "ValueError: stale legacy" not in msgs


def test_tail_child_errors_picks_later_date_on_identical_mtimes(tmp_path, monkeypatch):
    """F2 (portability regrade, 2026-09-06): the Linux server's filesystem
    gave two dated files written back-to-back the SAME mtime, so the strict
    `>` mtime comparison kept whichever one glob() happened to visit first
    (the older-dated file) — flipping test_tail_child_errors_reads_newest_
    dated_file_per_name from a happens-to-pass into a real failure. Ordering
    must key on the date embedded in the filename first, mtime only as a
    tie-break for two files sharing one date. glob() iteration order is
    filesystem-dependent (unlike the Linux box, this Mac happens to visit
    the newer file first, which would mask the bug), so it's pinned here to
    the adversarial old-first order the incident actually hit."""
    import os
    import time as time_mod
    from pathlib import Path as _Path

    monkeypatch.setenv("ARB_LOG_DIR", str(tmp_path))
    orig_glob = _Path.glob
    monkeypatch.setattr(_Path, "glob", lambda self, pat: iter(sorted(orig_glob(self, pat))))
    from arb.dashboard_mode import tail_child_errors
    cfg = _cfg()
    d = tmp_path / cfg["supervisor"]["child_log_dir"]
    d.mkdir(parents=True, exist_ok=True)
    old = d / "whale-backfill.2026-09-03.log"
    new = d / "whale-backfill.2026-09-05.log"
    old.write_text("Traceback (most recent call last):\nValueError: old day\n")
    new.write_text("Traceback (most recent call last):\nValueError: new day\n")
    same_time = time_mod.time()
    os.utime(old, (same_time, same_time))
    os.utime(new, (same_time, same_time))  # identical mtimes — date must decide

    errors = tail_child_errors(cfg)

    msgs = {e["msg"] for e in errors if e["source"] == "child:whale-backfill"}
    assert "ValueError: new day" in msgs
    assert "ValueError: old day" not in msgs


def test_tail_child_errors_prefers_dated_file_even_when_legacy_is_newer(tmp_path, monkeypatch):
    """F2 sibling: the fix must not merely swap which mtime wins — an
    undated legacy <name>.log ranks below EVERY dated file regardless of
    mtime, because a dated file is always the more recent write by
    definition (M8 rotates <name>.log out once dating starts)."""
    import os
    import time as time_mod

    monkeypatch.setenv("ARB_LOG_DIR", str(tmp_path))
    from arb.dashboard_mode import tail_child_errors
    cfg = _cfg()
    d = tmp_path / cfg["supervisor"]["child_log_dir"]
    d.mkdir(parents=True, exist_ok=True)
    dated = d / "whale.2026-09-05.log"
    dated.write_text("Traceback (most recent call last):\nValueError: dated\n")
    legacy = d / "whale.log"
    legacy.write_text("Traceback (most recent call last):\nValueError: legacy\n")
    newer = time_mod.time() + 3600
    os.utime(legacy, (newer, newer))  # legacy has a NEWER mtime than the dated file

    errors = tail_child_errors(cfg)

    msgs = {e["msg"] for e in errors if e["source"] == "child:whale"}
    assert "ValueError: dated" in msgs
    assert "ValueError: legacy" not in msgs


def test_lanes_and_venues_stay_visible_in_whale_mode_but_scoped(tmp_path):
    """dashboard.mode: whale keeps #lanes (all lanes, disabled ones labeled)
    and #venues (watch_venues only) rather than hiding either."""
    cfg = _cfg()
    cfg["dashboard"]["mode"] = "whale"
    store = Store(tmp_path / "t.sqlite")
    now = datetime.now(timezone.utc)
    store.insert("polls", {"started_at": now.isoformat(), "kind": "negrisk",
                           "n_quotes": 0, "n_opps": 0})
    poll_id = store.insert("polls", {"started_at": now.isoformat(), "kind": "watch",
                                     "n_quotes": 0, "n_opps": 0})
    for venue in ("polymarket", "sx", "kalshi"):
        store.insert("poll_venues", {"poll_id": poll_id, "venue": venue, "ok": 1,
                                     "n_quotes": 0, "latency_ms": 10})

    from arb.dashboard import state
    s = state(cfg, store)

    lanes = {ln["name"]: ln for ln in s["lanes"]}
    assert lanes["watch"]["enabled"] is True
    assert lanes["negrisk"]["enabled"] is False        # off today, history still shown
    assert {v["venue"] for v in s["venues"]} == {"polymarket"}   # watch_venues only

    cfg_full = copy.deepcopy(cfg)
    cfg_full["dashboard"]["mode"] = "full"
    s_full = state(cfg_full, store)
    assert {v["venue"] for v in s_full["venues"]} == {"polymarket", "sx", "kalshi"}


def test_lanes_table_labels_disabled_lanes_in_js():
    html = (ROOT / "arb" / "dashboard.html").read_text()
    script = html.split("<script>")[1]
    lanes_render = script[script.index("$('lanes').innerHTML"):
                          script.index("$('lanes').innerHTML") + 400]
    assert "ln.enabled" in lanes_render


def test_whale_mode_hides_the_remaining_arb_only_sections():
    """Fix pass on 5c9bbb5: schedule/classes/the duplicate open-positions
    table/left-on-the-table/capacity/recent-flags/cadence are arb-only and
    still showed in whale mode; #positions already carries WHALE rows so
    #open is hidden rather than filtered."""
    html = (ROOT / "arb" / "dashboard.html").read_text()
    script = html.split("<script>")[1]
    apply_mode = script[script.index("function applyMode("):script.index("applyMode();")]
    assert "closest('section').hidden = true" in apply_mode
    for el_id in ("schedule", "classes", "open", "lot", "capacity", "flags", "cadence"):
        assert f"'{el_id}'" in apply_mode, el_id
    # #lanes/#venues stay visible (labeled/filtered instead of hidden)
    assert "'lanes'" not in apply_mode and "'venues'" not in apply_mode


def test_full_mode_js_never_hides_the_arb_only_sections():
    """Companion to test_whale_mode_hides_the_remaining_arb_only_sections:
    applyMode's hides are guarded by an early return on MODE — in full mode
    (MODE === 'full') every hide below that guard is unreachable, so the
    seven ids stay visible. Checked the same way as the whale-mode test:
    at the source level, on the guard that makes it true, since MODE is
    templated server-side and there's no JS engine here to execute it."""
    html = (ROOT / "arb" / "dashboard.html").read_text()
    script = html.split("<script>")[1]
    apply_mode = script[script.index("function applyMode("):script.index("applyMode();")]
    first_stmt = apply_mode[apply_mode.index("{") + 1:].strip().splitlines()[0].strip()
    assert first_stmt == "if(MODE !== 'whale') return;"


def test_exit_rows_walk_their_own_root_set_not_rois():
    """drawWhale's exitRows used to reuse `roots` (built from f.roi), so an
    exits-only root absent from f.roi silently dropped its exit row."""
    html = (ROOT / "arb" / "dashboard.html").read_text()
    script = html.split("<script>")[1]
    draw = script[script.index("function drawWhale("):script.index("function drawTraders(")]
    exit_decl = draw[draw.index("const exitRows"):draw.index("const exitRows") + 200]
    assert "rootsFirst(f.exits" in exit_decl


def test_whale_mode_helpers_moved_to_dashboard_mode_and_dashboard_py_stays_lean():
    """5c9bbb5 fix pass finding: dashboard.py carried the whale-mode layout
    helpers alongside the HTTP handler. They move to arb/dashboard_mode.py;
    dashboard.py calls in thin."""
    from arb import dashboard, dashboard_mode
    dashboard_lines = len(Path("arb/dashboard.py").read_text().splitlines())
    assert dashboard_lines <= 300            # CLAUDE.md: modules stay ~300 lines
    # 150 -> 160 in the 2026-09-05 child-log fix pass (I2): _keep_error_line
    # learned to map child:<name> onto its supervisor.processes lane
    # 160 -> 170 in the review fix pass on THAT commit (798aae6): _WHALE_CORE
    # (M6) and tail_child_errors' newest-dated-file-per-name grouping (M8)
    # 170 -> 180 (F2, portability regrade 2026-09-06): newest-file selection
    # now keys on the filename's embedded date, mtime alone ties on some
    # filesystems (_log_recency_key)
    assert len(Path("arb/dashboard_mode.py").read_text().splitlines()) <= 180
    for name in ("_TITLES", "_WHALE_FRONT", "_whale_first", "_extract_marked"):
        assert not hasattr(dashboard, name)
    assert hasattr(dashboard_mode, "TITLES")
    assert hasattr(dashboard_mode, "WHALE_FRONT")
    assert hasattr(dashboard_mode, "whale_first")
    assert hasattr(dashboard_mode, "extract_marked")


def test_whale_metrics_report_whale_and_whale_mat_stay_under_300_lines():
    """Cleanup batch 2026-09-05: whale_metrics.py (330), report_whale.py
    (309) and whale_mat.py (412) each carried a helper pair over the CLAUDE.md
    ~300-line budget. realized_pnl/exits_recent moved to arb/whale_pnl.py,
    open_tape_ro moved to arb/whale_tape_ro.py, and whale_mat.py's read side
    (load_positions/load_hygiene/freshness/cache_partial/warn_if_stale) moved
    to arb/whale_mat_read.py — each re-exported so no caller moved."""
    for name in ("whale_metrics.py", "report_whale.py", "whale_mat.py"):
        lines = len(Path(f"arb/{name}").read_text().splitlines())
        assert lines <= 300, f"{name}: {lines} lines"
    from arb import report_whale, whale_mat, whale_metrics
    for mod, attr in ((whale_metrics, "realized_pnl"), (whale_metrics, "exits_recent"),
                      (report_whale, "open_tape_ro"), (whale_mat, "load_positions"),
                      (whale_mat, "load_hygiene"), (whale_mat, "freshness"),
                      (whale_mat, "cache_partial"), (whale_mat, "warn_if_stale")):
        assert hasattr(mod, attr)                    # re-exported, not just moved


def test_the_page_draws_its_own_charts_and_loads_nothing_remote():
    html = (ROOT / "arb" / "dashboard.html").read_text()
    assert "<svg" in html and "polyline" in html
    assert "http://" not in html and "https://" not in html
    assert "src=" not in html


def test_whale_panel_js_shows_the_stale_cache_note_when_present():
    # review of 783301f (I2): the frame carries collector.mat_stale ("stale (N behind,
    # M dirty)") but the page did not render it (matfix concern 2)
    html = Path("arb/dashboard.html").read_text()
    assert "c.mat_stale" in html and "cache: " in html
