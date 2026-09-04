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

from arb import bench, dashboard_data, dashboard_whale, schema
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
    assert schema.SCHEMA_VERSION == 8
    assert store.conn.execute(
        "SELECT MAX(version) FROM schema_version").fetchone()[0] == 8
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
    assert con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 8
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
        assert {r["variant"] for r in rows} == {"V1/flat", "V1/conf",
                                                "V6/flat", "V6/conf"}
        assert _get(base, "/api/lanes")["lanes"] == []
    finally:
        httpd.shutdown()


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


def test_the_page_draws_its_own_charts_and_loads_nothing_remote():
    html = (ROOT / "arb" / "dashboard.html").read_text()
    assert "<svg" in html and "polyline" in html
    assert "http://" not in html and "https://" not in html
    assert "src=" not in html
