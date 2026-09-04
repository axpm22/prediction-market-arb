"""L14 Task 9 — the whale section of the daily report, the per-variant class
key, and the one-sided cluster bootstrap (design 2026-09-03 §11, §12).

The ledger fixtures are Task 8's (tests/test_whale_lane.py): a tmp `Store` with
one watch poll plus a tmp tape, run through a real `whale --once` cycle wherever
the test needs the lane's own linkage (signal_id, exits, whale lists); crafted
rows where it needs a shape one cycle cannot reach (a legacy variant-NULL row,
a transfer-only whale, a resolved market under an exited bet).

Nothing here touches the network or the production DBs.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arb import report_whale
from arb.regrade import _bootstrap_mean_ci
from arb.report import daily_frame, print_daily, write_daily
from arb.store import Store

from tests.test_whale_lane import (CID, NOW, _flip, _ledger, _run, _tape,
                                   cfg)  # noqa: F401

DAY = "2026-09-03"                      # NOW is 13:00 ET on this day


# ------------------------------------------------------------------ builders

def _legacy(store, kind="EV", lane="clean", fp="fp-legacy", created=NOW):
    """A pre-L14 row: `variant` is NULL, exactly like every row in the live DB."""
    return store.insert("flat_bets", {
        "created_at": created.isoformat(), "poll_id": 1, "poll_kind": "watch",
        "fingerprint": fp, "position_key": fp, "kind": kind, "category": "sports",
        "sport": "baseball_mlb", "venue_pair": "polymarket:kalshi",
        "event_key": "mlb:legacy", "start_grade": "nominal",
        "legs_json": json.dumps([{"venue": "kalshi", "market_type": "WINNER",
                                  "outcome": "Rays", "side": "YES",
                                  "cost": "0.50", "fee_cost": "0.01"}]),
        "stake_usd": "1", "edge_points": "0.01", "trusted": 1, "lane": lane,
        "quality_flag": "none", "leg_skew_secs": 0.0, "status": "OPEN"})


def _whale_row(store, variant, signal_id, event_key, pnl=None, stake="1",
               created=NOW, cost="0.50", fee="0.01"):
    """One booked WHALE row; `pnl` not None also appends its settled event."""
    fp = f"fp-{variant}-{signal_id}-{event_key}"
    bet_id = store.insert("flat_bets", {
        "created_at": created.isoformat(), "poll_id": 1, "poll_kind": "whale",
        "fingerprint": fp, "position_key": fp, "kind": "WHALE",
        "category": "sports", "sport": "baseball_mlb",
        "venue_pair": "polymarket:self", "event_key": event_key,
        "event_start_utc": (created + timedelta(hours=3)).isoformat(),
        "start_grade": "nominal",
        "legs_json": json.dumps([{"venue": "polymarket", "market_type": "WINNER",
                                  "outcome": "Rays", "side": "YES", "cost": cost,
                                  "fee_cost": fee, "mid0": "0.50",
                                  "venue_market_id": f"{CID}:0xyes"}]),
        "stake_usd": stake, "edge_points": "0.05", "trusted": 0, "lane": "review",
        "quality_flag": "none", "leg_skew_secs": 0.0, "status": "OPEN",
        "variant": variant, "signal_id": signal_id})
    if pnl is not None:
        store.insert("bet_events", {
            "bet_id": bet_id, "created_at": created.isoformat(), "kind": "settled",
            "payload_json": json.dumps({"result": "EXITED", "source": "exit:flip",
                                        "pnl": str(pnl)})})
    return bet_id


def _signal_row(con, signal_id, whales, variant="V2", side=0, fired=1,
                reason=None, at=NOW):
    con.execute(
        "INSERT INTO signals (id, evaluated_at, condition_id, side, variant, "
        "fired, suppressed_reason, whale_ids_json) VALUES (?,?,?,?,?,?,?,?)",
        (signal_id, at.isoformat(), CID, str(side), variant, fired, reason,
         json.dumps(whales)))
    con.commit()


def _whale(wallet, native=True, side=0):
    return {"wallet": wallet, "side": side, "lb": "0.05", "n_bucket": 4,
            "native": native}


# ------------------------------------------------------- the by_class key

def test_daily_frame_groups_by_kind_lane_and_variant(cfg, tmp_path):
    """§11: the class key becomes (kind, lane, variant). Two WHALE rows of the
    same kind+lane and different variants are two cells, never one."""
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a")
    _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")
    _legacy(store)

    frame = daily_frame(cfg, store, DAY)

    assert set(frame["by_class"]) == {("WHALE", "review", "V2/flat"),
                                      ("WHALE", "review", "V2/conf"),
                                      ("EV", "clean", "")}
    assert all(a["bets"] == 1 for a in frame["by_class"].values())


def test_legacy_rows_group_exactly_as_before(cfg, tmp_path):
    """Every existing row has variant NULL, so they must land in ONE cell."""
    store = Store(tmp_path / "t.sqlite")
    _legacy(store, fp="fp-1")
    _legacy(store, fp="fp-2")

    frame = daily_frame(cfg, store, DAY)

    assert list(frame["by_class"]) == [("EV", "clean", "")]
    assert frame["by_class"][("EV", "clean", "")]["bets"] == 2


def test_write_daily_renders_the_variant_column_and_the_whale_section(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a")
    _legacy(store)

    frame = write_daily(cfg, store, DAY, outdir=tmp_path / "out")

    md = (tmp_path / "out" / f"{DAY}.md").read_text()
    header = [ln for ln in md.splitlines() if ln.startswith("| class |")][0]
    assert "variant" in header
    assert "| WHALE | review | V2/flat |" in md
    assert "| EV | clean |  |" in md                 # legacy: empty cell
    assert "whale-consensus lane" in md
    assert "whale" in frame


def test_print_daily_runs_with_a_whale_row_and_a_legacy_row(cfg, tmp_path, capsys):
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a")
    _legacy(store)

    print_daily(cfg, store, DAY)

    assert "V2/flat" in capsys.readouterr().out


def test_dashboard_state_carries_the_variant(cfg, tmp_path):
    """dashboard.state unpacks the same key; a WHALE row and a legacy row must
    both survive it."""
    from arb.dashboard import state
    now = datetime.now(timezone.utc)
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a", created=now)
    _legacy(store, created=now)

    rows = {(r["kind"], r["variant"]): r for r in state(cfg, store)["by_class"]}

    assert rows[("WHALE", "V2/flat")]["bets"] == "1"
    assert rows[("EV", "")]["bets"] == "1"


# ------------------------------------------------------- the whale section

def test_the_whale_section_renders_on_an_empty_tape(cfg, tmp_path):
    """No prints, no signals, no bets: the section is still produced, with
    zeros, and says the lane has no sample yet."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)
    md = "\n".join(report_whale.whale_md(frame))

    assert frame["wallets"]["tracked"] == 0
    assert frame["signals"]["evaluated_24h"] == 0
    assert frame["roi"] == {}
    assert "whale-consensus lane" in md


def test_the_section_renders_without_a_tape_at_all(cfg, tmp_path):
    """`arb report` runs on a machine where the collector has never started."""
    cfg["whale"]["tape_db"] = str(tmp_path / "does-not-exist.sqlite")
    store = Store(tmp_path / "t.sqlite")

    frame = report_whale.whale_frame(cfg, store, now=NOW)

    assert frame["tape_ok"] is False
    assert "tape" in "\n".join(report_whale.whale_md(frame)).lower()


def test_fires_are_counted_by_distinct_signal_id_not_signals_fired(cfg, tmp_path):
    """A fire re-evaluates every cycle until the prints age out, so `signals`
    holds ~5 fired rows per booking. The report counts DISTINCT `signal_id`
    values in flat_bets — the bookings — never `signals.fired`."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    for sid in range(1, 6):                        # five fired evaluations
        _signal_row(con, sid, [_whale("w1")], at=NOW - timedelta(seconds=60 * sid))
    _whale_row(store, "V2/flat", 1, "pm:a")        # one booking, two rows
    _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert con.execute("SELECT SUM(fired) s FROM signals").fetchone()["s"] == 5
    assert frame["signals"]["evaluated_24h"] == 5
    assert frame["signals"]["fired_24h"] == 1
    assert frame["signals"]["fired_7d"] == 1


def test_suppressed_signals_are_reported_by_reason_and_variant(cfg, tmp_path):
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    _signal_row(con, 1, [], variant="V2", fired=0, reason="uncalibrated")
    _signal_row(con, 2, [], variant="V3", fired=0, reason="uncalibrated")
    _signal_row(con, 3, [], variant="V1", fired=0, reason="min-points")

    sup = report_whale.whale_frame(cfg, store, now=NOW, con=con)["signals"]["suppressed"]

    assert sup[("uncalibrated", "V2")] == 1
    assert sup[("uncalibrated", "V3")] == 1
    assert sup[("min-points", "V1")] == 1


def test_an_exited_bet_whose_market_resolved_gets_a_hold_counterfactual(cfg, tmp_path):
    """§12 'exits by reason with exit-vs-hold P&L': the counterfactual is the
    resolution the tape recorded AFTER the exit, priced on the same contracts
    the exit was priced on."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    _run(cfg, store, con, now=NOW + timedelta(seconds=60))
    con.execute("INSERT INTO resolutions (condition_id, resolved_at, "
                "winner_outcome_index, status) VALUES (?,?,?,?)",
                (CID, (NOW + timedelta(hours=6)).isoformat(), 0, "resolved"))
    con.commit()

    exits = report_whale.whale_frame(cfg, store, now=NOW + timedelta(hours=7),
                                     con=con)["exits"]

    assert exits["flip"]["n"] == 2 and exits["flip"]["n_hold"] == 2
    # exit: two bets at the 0.49 bid; hold: the YES side won, so each bet's
    # contracts pay $1 (contracts = stake / (0.51 + 0.007497))
    assert abs(exits["flip"]["exit_pnl"] - Decimal("-0.091500")) < Decimal("0.001")
    assert abs(exits["flip"]["hold_pnl"] - Decimal("1.261581")) < Decimal("0.001")


def test_a_losing_hold_counterfactual_is_negative(cfg, tmp_path):
    """The other side of the same computation: the exit saved money."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    _run(cfg, store, con, now=NOW + timedelta(seconds=60))
    con.execute("INSERT INTO resolutions (condition_id, resolved_at, "
                "winner_outcome_index, status) VALUES (?,?,?,?)",
                (CID, (NOW + timedelta(hours=6)).isoformat(), 1, "resolved"))
    con.commit()

    exits = report_whale.whale_frame(cfg, store, now=NOW + timedelta(hours=7),
                                     con=con)["exits"]

    assert exits["flip"]["hold_pnl"] == Decimal("-1.353080")     # both stakes lost


# ------------------------------------------------------------- the bootstrap

def test_bootstrap_percentiles_are_parameterised():
    """§12: every interval in the whale section is one-sided, so the bootstrap
    takes its percentile pair. The two-sided default stays for regrade."""
    by_event = {f"e{i}": [float(i % 7) / 10 - 0.3] for i in range(60)}

    mean, lo_d, hi_d = _bootstrap_mean_ci(by_event, 7, 400)
    mean9, lo9, hi9 = _bootstrap_mean_ci(by_event, 7, 400, percentiles=(0.10, 0.90))

    assert mean == mean9
    assert lo_d < lo9 < hi9 < hi_d          # 95% two-sided brackets the 10/90


def test_bootstrap_default_is_unchanged_for_existing_callers():
    by_event = {f"e{i}": [float(i % 5)] for i in range(40)}
    explicit = _bootstrap_mean_ci(by_event, 3, 200, percentiles=(0.025, 0.975))
    assert _bootstrap_mean_ci(by_event, 3, 200) == explicit


# ------------------------------------------------------------- ROI and split

def test_roi_is_reported_per_variant_with_one_sided_bounds(cfg, tmp_path):
    """§12: mean settled ROI per $ staked, clustered by event_key, with the
    20th (lower 80%) and 90th (upper 90%) percentiles of the cluster
    bootstrap. V2/flat is the primary; every variant is reported."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    for i in range(8):
        _whale_row(store, "V2/flat", i + 1, f"pm:e{i}", pnl=Decimal("0.20"))
        _whale_row(store, "V1/flat", i + 1, f"pm:e{i}", pnl=Decimal("-0.10"))

    roi = report_whale.whale_frame(cfg, store, now=NOW, con=con)["roi"]

    assert roi["V2/flat"]["n"] == 8 and roi["V2/flat"]["n_events"] == 8
    assert roi["V2/flat"]["mean"] == Decimal("0.200000")
    assert roi["V2/flat"]["lb80"] <= roi["V2/flat"]["mean"] <= roi["V2/flat"]["ub90"]
    assert roi["V2/flat"]["win_rate"] == Decimal("1.000000")
    assert roi["V1/flat"]["mean"] == Decimal("-0.100000")
    assert roi["V1/flat"]["win_rate"] == Decimal("0")


def test_the_roi_bounds_are_the_one_sided_percentiles_spec_12_names(cfg, tmp_path):
    """lb80 is the 20th percentile of the cluster bootstrap and ub90 the 90th —
    NOT the two-sided 95% pair every other caller of the bootstrap uses."""
    from arb.models import q6
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    vals = [0.20 if i % 2 else -0.10 for i in range(12)]
    for i, v in enumerate(vals):
        _whale_row(store, "V2/flat", i + 1, f"pm:e{i}", pnl=Decimal(str(v)))

    roi = report_whale.whale_frame(cfg, store, now=NOW, con=con)["roi"]["V2/flat"]

    by_event = {f"pm:e{i}": [v] for i, v in enumerate(vals)}
    seed, n_boot = (int(cfg["regrade"]["bootstrap_seed"]),
                    int(cfg["regrade"]["bootstrap_n"]))
    _, lo10, hi90 = _bootstrap_mean_ci(by_event, seed, n_boot, percentiles=(0.10, 0.90))
    _, lo20, hi80 = _bootstrap_mean_ci(by_event, seed, n_boot, percentiles=(0.20, 0.80))
    two_sided = _bootstrap_mean_ci(by_event, seed, n_boot)
    assert (roi["ub90"], roi["lo90"]) == (q6(Decimal(hi90)), q6(Decimal(lo10)))
    assert (roi["lb80"], roi["hi80"]) == (q6(Decimal(lo20)), q6(Decimal(hi80)))
    assert roi["ub90"] != q6(Decimal(two_sided[2]))     # not the two-sided pair


def test_two_bets_on_one_event_are_one_bootstrap_cluster(cfg, tmp_path):
    """Rule-11 clustering: event_key, not bet, is the independent unit."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    for i in range(6):
        _whale_row(store, "V2/flat", 2 * i + 1, "pm:same", pnl=Decimal("0.10"))

    roi = report_whale.whale_frame(cfg, store, now=NOW, con=con)["roi"]["V2/flat"]

    assert roi["n"] == 6 and roi["n_events"] == 1
    assert roi["lb80"] == roi["ub90"] == roi["mean"]   # one cluster: no spread


def test_the_section_splits_signals_and_roi_by_native_and_transfer(cfg, tmp_path):
    """§7: a signal is native only when EVERY contributing whale on the fired
    side qualified on its own bucket; otherwise it rode pooled evidence."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    _signal_row(con, 1, [_whale("w1"), _whale("w2")])
    _signal_row(con, 2, [_whale("w3"), _whale("w4", native=False)])
    _whale_row(store, "V2/flat", 1, "pm:a", pnl=Decimal("0.30"))
    _whale_row(store, "V2/flat", 2, "pm:b", pnl=Decimal("-0.10"))

    split = report_whale.whale_frame(cfg, store, now=NOW, con=con)["native"]

    assert split["signals"] == {"native": 1, "transfer": 1}
    assert split["roi"]["native"]["n"] == 1
    assert split["roi"]["native"]["mean"] == Decimal("0.300000")
    assert split["roi"]["transfer"]["mean"] == Decimal("-0.100000")


def test_a_whale_on_the_other_side_never_makes_a_signal_transfer(cfg, tmp_path):
    """Only contributors on the FIRED side carry the native flag that counts."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    _signal_row(con, 1, [_whale("w1"), _whale("w2", native=False, side=1)])
    _whale_row(store, "V2/flat", 1, "pm:a", pnl=Decimal("0.30"))

    split = report_whale.whale_frame(cfg, store, now=NOW, con=con)["native"]

    assert split["signals"] == {"native": 1, "transfer": 0}


# ---------------------------------------------------------------- markouts

def test_markout_coverage_and_mean_drift_are_reported_pregame_only(cfg, tmp_path):
    """§12: +2m and +10m coverage and mean drift, in_play marks excluded."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    a = _whale_row(store, "V2/flat", 1, "pm:a")
    b = _whale_row(store, "V2/flat", 2, "pm:b")
    for bet_id, cost in ((a, "0.52"), (b, "0.54")):          # mid0 = 0.50
        store.insert("markouts", {"bet_id": bet_id, "leg_index": 0,
                                  "horizon_secs": 120, "actual_offset_secs": 120.0,
                                  "mark_cost": cost, "marked_at": NOW.isoformat(),
                                  "in_play": 0})
    store.insert("markouts", {"bet_id": a, "leg_index": 0, "horizon_secs": 600,
                              "actual_offset_secs": 600.0, "mark_cost": "0.40",
                              "marked_at": NOW.isoformat(), "in_play": 1})

    marks = report_whale.whale_frame(cfg, store, now=NOW, con=con)["markouts"]

    assert marks[120]["n"] == 2 and marks[120]["coverage"] == Decimal("1.000000")
    assert marks[120]["mean_drift"] == Decimal("0.030000")   # (0.02 + 0.04)/2
    assert marks[600]["n"] == 0                              # in_play excluded
    assert marks[600]["coverage"] == Decimal("0")


# -------------------------------------------------------- collector health

def test_collector_health_reports_the_last_cycle_the_gap_and_the_backoff(cfg, tmp_path):
    """The operator-facing half of §12: is the tape being written at all?"""
    from arb.whale_tape import _set_meta
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    _set_meta(con, "last_cycle", json.dumps({"status": "ok", "prints": 41,
                                             "bad_rows": 2}))
    _set_meta(con, "last_seen_ts", int((NOW - timedelta(seconds=90)).timestamp()))
    _set_meta(con, "last_gap", json.dumps({"secs": 300, "at": NOW.isoformat()}))
    _set_meta(con, "backoff_until", (NOW + timedelta(seconds=60)).isoformat())
    con.execute("INSERT INTO prints (observed_at, ts, proxy_wallet, condition_id, "
                "asset, outcome_index, side, price, size, notional_usd) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (NOW.isoformat(), int((NOW - timedelta(minutes=30)).timestamp()),
                 "w1", CID, "0xyes", 0, "BUY", "0.50", "1000", "500"))
    con.commit()

    health = report_whale.whale_frame(cfg, store, now=NOW, con=con)["collector"]

    assert health["status"] == "ok" and health["bad_rows"] == 2
    assert health["rows"] == 41
    assert 89 <= health["last_seen_age_secs"] <= 91
    assert health["last_gap"]["secs"] == 300
    assert health["backoff_until"] is not None
    assert health["prints_per_min"] == Decimal("0.016667")    # 1 print in 60 min


def test_calibration_status_shows_the_null_thresholds_and_the_day_count(cfg, tmp_path):
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    for i, day in enumerate((1, 2, 2, 3)):
        _signal_row(con, i + 1, [], at=datetime(2026, 8, day, 12, tzinfo=timezone.utc))

    cal = report_whale.whale_frame(cfg, store, now=NOW, con=con)["calibration"]

    assert cal["min_points"]["V2"] is None and cal["min_points"]["V1"] is not None
    assert cal["signal_days"] == 3
    assert cal["calibration_days"] == int(cfg["whale"]["fire"]["calibration_days"])


# --------------------------------------------------------- qualifying whales

def test_qualifying_whales_come_from_the_lane_s_own_scoring_assembly(cfg, tmp_path):
    """The report must price the same whales the lane does — without running
    any of the lane's network steps."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    top = frame["qualifying"]["top"]
    assert len(top) == 3                             # the cycle's three whales
    assert {w["wallet"] for w in top} == {"w0", "w1", "w2"}
    assert all(w["n_bucket"] == 0 for w in top)      # no resolved history yet
    assert frame["qualifying"]["by_sport"]["baseball_mlb"] == 3
    assert frame["qualifying"]["by_root"]["sports"] == 3
    assert frame["wallets"]["hygiene_excluded"] == 0


def test_wallet_counts_come_from_the_tape(cfg, tmp_path):
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    for wallet, done in (("w1", NOW.isoformat()), ("w2", None), ("w3", None)):
        con.execute("INSERT INTO wallets (proxy_wallet, first_seen, source, "
                    "backfilled_at) VALUES (?,?,?,?)",
                    (wallet, NOW.isoformat(), "tape", done))
    con.commit()

    wallets = report_whale.whale_frame(cfg, store, now=NOW, con=con)["wallets"]

    assert wallets == {"tracked": 3, "backfilled": 1, "hygiene_excluded": 0}


def test_the_report_never_polls_the_network(cfg, tmp_path, monkeypatch):
    """Rule 1 and the read-only contract: no print poll, no book fetch, no
    resolution read from the report path."""
    from arb import whale_resolve, whale_tape
    from arb.venues import polymarket
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    for mod, name in ((whale_tape, "poll_prints"), (whale_resolve, "read_resolutions"),
                      (polymarket, "fetch_books")):
        monkeypatch.setattr(mod, name,
                            lambda *a, **k: pytest.fail(f"{name} ran in the report"))

    report_whale.whale_frame(cfg, store, now=NOW, con=con)


def test_the_report_writes_nothing_to_the_tape(cfg, tmp_path):
    """Rule 9's spirit: the section is a read. Base rates are the LANE's write."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    before = con.execute("SELECT COUNT(*) c FROM base_rates").fetchone()["c"]

    report_whale.whale_frame(cfg, store, now=NOW + timedelta(hours=1), con=con)

    assert con.execute("SELECT COUNT(*) c FROM base_rates").fetchone()["c"] == before
