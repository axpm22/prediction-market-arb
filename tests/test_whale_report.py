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
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arb import report_whale
from arb.regrade import _bootstrap_mean_ci
from arb.report import daily_frame, print_daily, write_daily
from arb.store import Store

from tests.test_whale_lane import (CID, NOW, _flip, _holding, _ledger, _run,
                                   _tape, cfg)  # noqa: F401

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
               created=NOW, cost="0.50", fee="0.01", category="sports"):
    """One booked WHALE row; `pnl` not None also appends its settled event.
    `category` IS the root (§8, amended 2026-09-04) — rows from different roots
    are never pooled."""
    fp = f"fp-{variant}-{signal_id}-{event_key}"
    bet_id = store.insert("flat_bets", {
        "created_at": created.isoformat(), "poll_id": 1, "poll_kind": "whale",
        "fingerprint": fp, "position_key": fp, "kind": "WHALE",
        "category": category,
        "sport": "baseball_mlb" if category == "sports" else category,
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


def _whale(wallet, native=True, side=0, sybil_dropped=False):
    return {"wallet": wallet, "side": side, "lb": "0.05", "n_bucket": 4,
            "native": native, "sybil_dropped": sybil_dropped}


def test_the_report_ranks_traders_and_says_where_each_is_good(cfg, tmp_path):
    """Design §12 "Trader profiles": read-time diagnostics in the L14 section,
    deterministic at the day's end, never an input to a fire rule."""
    from arb import whale_tape
    from tests.test_whale_rank import _tape as _rank_tape
    store = Store(tmp_path / "t.sqlite")
    con = whale_tape.open_tape(_rank_tape(tmp_path, cfg, now=NOW))

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert [r["wallet"] for r in frame["traders"]["top"]] == ["0xwhale"]
    assert frame["traders"]["best"]["0xwhale"][0]["key"] == "basketball_nba:WINNER"
    md = "\n".join(report_whale.whale_md(frame))
    assert "Top traders" in md
    assert "| 0xwhale | basketball_nba:WINNER | 6 | 83.3% | 60.0% | 23.3% |" in md
    assert "where **0xwhale** is good: basketball_nba:WINNER" in md
    # a re-run of the same day reproduces the same bytes (rule 9)
    assert md == "\n".join(report_whale.whale_md(
        report_whale.whale_frame(cfg, store, now=NOW, con=con)))


def test_the_trader_table_is_empty_but_present_on_a_tape_with_no_history(cfg, tmp_path):
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)

    md = "\n".join(report_whale.whale_md(
        report_whale.whale_frame(cfg, store, now=NOW, con=con)))

    assert "Top traders" in md and "| — | — | 0 |" in md


def test_the_variant_table_reports_v4_beside_the_other_variants(cfg, tmp_path):
    """L14 amendment 2026-09-04: V4 is a secondary variant, reported with the
    same one-sided bounds as V1/V3. The primary stays V2/flat."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    _whale_row(store, "V4/flat", 1, "pm:a", pnl="0.10")
    _whale_row(store, "V2/flat", 2, "pm:b", pnl="0.20")

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert set(frame["roi"]["sports"]) == {"V2/flat", "V4/flat"}
    assert frame["primary_variant"] == "V2/flat"
    md = "\n".join(report_whale.whale_md(frame))
    assert "| V4/flat |" in md and "| V2/flat **(primary)** |" in md
    assert "V4=3" in md                      # the calibration line names it too


# ------------------------------------------------------- the by_class key

def test_daily_frame_groups_by_kind_lane_and_variant(cfg, tmp_path):
    """§11: the class key becomes (kind, lane, variant). Two WHALE rows of the
    same kind+lane and different variants are two cells, never one."""
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a")
    _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")
    _whale_row(store, "V2/kelly", 1, "pm:a", stake="0.25")
    _legacy(store)

    frame = daily_frame(cfg, store, DAY)

    assert set(frame["by_class"]) == {("WHALE", "review", "V2/flat"),
                                      ("WHALE", "review", "V2/conf"),
                                      ("WHALE", "review", "V2/kelly"),
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
    # the entry holders gate (§7 item 1b, 2026-09-04) reports like every other
    # reason — the breakdown is keyed on whatever the lane wrote
    _signal_row(con, 4, [], variant="V1", fired=0, reason="holders")

    sup = report_whale.whale_frame(cfg, store, now=NOW, con=con)["signals"]["suppressed"]

    assert sup[("uncalibrated", "V2")] == 1
    assert sup[("uncalibrated", "V3")] == 1
    assert sup[("min-points", "V1")] == 1
    assert sup[("holders", "V1")] == 1


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
                                     con=con)["exits"]["sports"]

    # six bets: V1 and V6 (added 2026-09-04) each book all three sizings
    assert exits["flip"]["n"] == 6 and exits["flip"]["n_hold"] == 6
    # exit: six bets at the 0.49 bid; hold: the YES side won, so each bet's
    # contracts pay $1 (contracts = stake / (0.51 + 0.007497))
    assert abs(exits["flip"]["exit_pnl"] - Decimal("-0.196520")) < Decimal("0.001")
    assert abs(exits["flip"]["hold_pnl"] - Decimal("2.709640")) < Decimal("0.001")


def test_left_exits_are_broken_down_by_the_reading_that_fired_them(cfg, tmp_path):
    """Item D of the L14 amendment (2026-09-04): `left` has two readings now —
    the print tape and the venue's own `positions?user=` — and the
    pre-registered exit-vs-hold table has to say which one fired, or the two
    can never be compared against the hold counterfactual separately."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    holdings = {f"w{i}": [_holding(f"w{i}", 400)] for i in range(3)}
    _run(cfg, store, con, now=NOW + timedelta(seconds=60), rows=[],
         positions=holdings)

    exits = report_whale.whale_frame(cfg, store, now=NOW + timedelta(hours=7),
                                     con=con)["exits"]["sports"]

    assert set(exits) == {"left (positions)"}
    assert exits["left (positions)"]["n"] == 6


def test_an_exited_bet_whose_market_later_voided_gets_a_zero_hold(cfg, tmp_path):
    """Ledger convention (arb/metrics.py: 'VOID leg contributes 0'): a void
    resolution is a valid hold outcome, not a missing one — it belongs in the
    exit-vs-hold comparison with hold P&L 0.000000, not dropped from n_hold."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    _run(cfg, store, con, now=NOW + timedelta(seconds=60))
    con.execute("INSERT INTO resolutions (condition_id, resolved_at, "
                "winner_outcome_index, status) VALUES (?,?,?,?)",
                (CID, (NOW + timedelta(hours=6)).isoformat(), None, "void"))
    con.commit()

    exits = report_whale.whale_frame(cfg, store, now=NOW + timedelta(hours=7),
                                     con=con)["exits"]["sports"]

    assert exits["flip"]["n_hold"] == 6
    assert exits["flip"]["hold_pnl"] == Decimal("0.000000")


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
                                     con=con)["exits"]["sports"]

    assert exits["flip"]["hold_pnl"] == Decimal("-2.906160")     # all six stakes lost


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

    roi = report_whale.whale_frame(cfg, store, now=NOW, con=con)["roi"]["sports"]

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

    roi = report_whale.whale_frame(cfg, store, now=NOW, con=con)["roi"]["sports"]["V2/flat"]

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

    roi = report_whale.whale_frame(cfg, store, now=NOW, con=con)["roi"]["sports"]["V2/flat"]

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


def test_a_sybil_dropped_whale_never_flips_a_native_signal_to_transfer(cfg, tmp_path):
    """A sybil-dropped whale contributes zero points (arb/whale_signal.py:
    `points()` skips it) — it must not count toward `is_native` either. A
    duplicate print collapsed by the sybil rule must never turn an otherwise
    all-native fired side into 'transfer'."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    _signal_row(con, 1, [_whale("w1", native=True),
                        _whale("w2", native=False, sybil_dropped=True)])
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

    marks = report_whale.whale_frame(cfg, store, now=NOW, con=con)["markouts"]["sports"]

    assert marks[120]["n"] == 2 and marks[120]["coverage"] == Decimal("1.000000")
    assert marks[120]["mean_drift"] == Decimal("0.030000")   # (0.02 + 0.04)/2
    assert marks[600]["n"] == 0                              # in_play excluded
    assert marks[600]["coverage"] == Decimal("0")


# ------------------------------------------- markout dedup (9e4158d review)

def _mark_bet(store, bet_id, cost, horizon=120):
    """One pregame markout row for `bet_id`, mid0=0.50 as `_whale_row` bakes
    it — `cost` alone decides the drift."""
    store.insert("markouts", {"bet_id": bet_id, "leg_index": 0,
                              "horizon_secs": horizon, "actual_offset_secs": 120.0,
                              "mark_cost": cost, "marked_at": NOW.isoformat(),
                              "in_play": 0})


def test_three_sizing_rows_of_one_signal_mark_as_one_not_three(cfg, tmp_path):
    """A fired signal books V*/flat, V*/conf and V*/kelly (§8) sharing one
    signal_id and the same leg/price. Marking all three must not triple-count
    the signal in the pregame drift table."""
    from arb.whale_metrics import H_2M, markout_stats, pregame_drifts
    store = Store(tmp_path / "t.sqlite")
    flat = _whale_row(store, "V2/flat", 1, "pm:a")
    conf = _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")
    kelly = _whale_row(store, "V2/kelly", 1, "pm:a", stake="0.25")
    for bet_id in (flat, conf, kelly):
        _mark_bet(store, bet_id, "0.52")                     # drift 0.02 on all three

    drifts = pregame_drifts(store, (H_2M,))
    cell = markout_stats(store)["sports"][H_2M]

    assert len(drifts) == 1                                  # was 3
    assert cell["n"] == 1                                     # was 3
    assert cell["mean_drift"] == Decimal("0.020000")          # unchanged by dedup


def test_markout_dedup_prefers_the_flat_sizing_row(cfg, tmp_path):
    """The flat row is the registered "$1 per flag" convention — it wins as
    the representative even when the other sizings mark differently (never
    happens on the real leg, since all three share one price, but proves the
    selection rule rather than an accident of row order)."""
    from arb.whale_metrics import H_2M, markout_stats
    store = Store(tmp_path / "t.sqlite")
    conf = _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")
    kelly = _whale_row(store, "V2/kelly", 1, "pm:a", stake="0.25")
    flat = _whale_row(store, "V2/flat", 1, "pm:a")
    _mark_bet(store, conf, "0.60")                            # drift 0.10
    _mark_bet(store, kelly, "0.65")                           # drift 0.15
    _mark_bet(store, flat, "0.55")                            # drift 0.05 <- wins

    cell = markout_stats(store)["sports"][H_2M]

    assert cell["n"] == 1
    assert cell["mean_drift"] == Decimal("0.050000")


def test_markout_dedup_falls_back_to_any_sizing_without_a_flat_row(cfg, tmp_path):
    """A signal_id whose flat row never marked (e.g. still open) still counts
    once off whichever sizing did mark, not zero and not twice."""
    from arb.whale_metrics import H_2M, markout_stats
    store = Store(tmp_path / "t.sqlite")
    conf = _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")
    kelly = _whale_row(store, "V2/kelly", 1, "pm:a", stake="0.25")
    _whale_row(store, "V2/flat", 1, "pm:a")                   # never marked
    _mark_bet(store, conf, "0.58")
    _mark_bet(store, kelly, "0.62")

    cell = markout_stats(store)["sports"][H_2M]

    assert cell["n"] == 1
    assert cell["mean_drift"] in (Decimal("0.080000"), Decimal("0.120000"))


def test_markout_dedup_counts_distinct_signals_not_rows(cfg, tmp_path):
    """The bug's real-world shape: one over-sized signal (3 sizing rows) must
    not outweigh a plain one (1 row) in the pooled mean — each signal is one
    vote, whatever it was staked at."""
    from arb.whale_metrics import H_2M, markout_stats
    store = Store(tmp_path / "t.sqlite")
    a_flat = _whale_row(store, "V2/flat", 1, "pm:a")
    a_conf = _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")
    a_kelly = _whale_row(store, "V2/kelly", 1, "pm:a", stake="0.25")
    b_flat = _whale_row(store, "V2/flat", 2, "pm:b")
    for bet_id in (a_flat, a_conf, a_kelly):
        _mark_bet(store, bet_id, "0.52")                      # drift 0.02
    _mark_bet(store, b_flat, "0.60")                          # drift 0.10

    cell = markout_stats(store)["sports"][H_2M]

    assert cell["n"] == 2                                     # was 4
    assert cell["mean_drift"] == Decimal("0.060000")          # (0.02+0.10)/2, was 0.04


def test_kill_rule_2_n_marked_counts_signals_not_sizing_rows(cfg, tmp_path):
    """§12 rule 2 gates on `fast_kill_min_marked` marked SIGNALS. Two real
    signals booked at three sizings each must read n_marked=2, not 6 — the
    finding: the bug let the gate fire on a third of the intended evidence."""
    cfg["whale"]["kill"]["fast_kill_min_marked"] = 2
    store = Store(tmp_path / "t.sqlite")
    for sid, event in ((1, "pm:a"), (2, "pm:b")):
        flat = _whale_row(store, "V2/flat", sid, event)
        conf = _whale_row(store, "V2/conf", sid, event, stake="0.5")
        kelly = _whale_row(store, "V2/kelly", sid, event, stake="0.25")
        for bet_id in (flat, conf, kelly):
            _mark_bet(store, bet_id, "0.45")                  # drift -0.05

    rule = _rules(cfg, store)[2]

    assert rule["n_marked"] == 2                              # was 6
    assert rule["held_today"] is True                         # 2 >= min_marked=2


def test_kill_rule_2_does_not_meet_the_floor_on_row_count_alone(cfg, tmp_path):
    """The inverse of the above: one real signal (three sizing rows) must NOT
    clear a min_marked=2 floor — the pre-fix bug would have read n_marked=3
    and wrongly held the rule on a single signal's evidence."""
    cfg["whale"]["kill"]["fast_kill_min_marked"] = 2
    store = Store(tmp_path / "t.sqlite")
    flat = _whale_row(store, "V2/flat", 1, "pm:a")
    conf = _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")
    kelly = _whale_row(store, "V2/kelly", 1, "pm:a", stake="0.25")
    for bet_id in (flat, conf, kelly):
        _mark_bet(store, bet_id, "0.45")

    rule = _rules(cfg, store)[2]

    assert rule["n_marked"] == 1                              # was 3
    assert rule["held_today"] is False


def test_markout_dedup_ignores_legacy_signal_id_less_rows(cfg, tmp_path):
    """A pre-L14-sizing row (signal_id NULL) has no sibling sizing rows to
    collapse into — it must still count once, keyed by its own bet_id, exactly
    as before this fix."""
    from arb.whale_metrics import H_2M, markout_stats
    store = Store(tmp_path / "t.sqlite")
    a = _whale_row(store, "V2/flat", None, "pm:a")
    b = _whale_row(store, "V2/flat", None, "pm:b")
    _mark_bet(store, a, "0.52")
    _mark_bet(store, b, "0.54")

    cell = markout_stats(store)["sports"][H_2M]

    assert cell["n"] == 2
    assert cell["mean_drift"] == Decimal("0.030000")


# ------------------------------------------- coverage denominator (this fix)

def test_coverage_denominator_counts_the_signal_once_not_its_three_rows(cfg, tmp_path):
    """The numerator (`n`) was already deduped to one marked signal (e618139);
    the denominator was not — it still counted every `V*/flat`+`V*/conf`+
    `V*/kelly` row, so one fired signal marked on all three sizings read
    coverage 1-in-3 instead of 1.0. Both sides of the ratio must count the
    same population: signals, not booking rows."""
    from arb.whale_metrics import H_2M, markout_stats
    store = Store(tmp_path / "t.sqlite")
    flat = _whale_row(store, "V2/flat", 1, "pm:a")
    conf = _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")
    kelly = _whale_row(store, "V2/kelly", 1, "pm:a", stake="0.25")
    for bet_id in (flat, conf, kelly):
        _mark_bet(store, bet_id, "0.52")

    cell = markout_stats(store)["sports"][H_2M]

    assert cell["coverage"] == Decimal("1.000000")             # was 0.333333


def test_coverage_denominator_counts_two_fired_signals_one_marked(cfg, tmp_path):
    """Two real signals booked (one at all three sizings, one at one) — only
    one marks. Coverage is marked signals over FIRED signals: 1/2, not
    1-in-4 booking rows."""
    from arb.whale_metrics import H_2M, markout_stats
    store = Store(tmp_path / "t.sqlite")
    a_flat = _whale_row(store, "V2/flat", 1, "pm:a")
    _whale_row(store, "V2/conf", 1, "pm:a", stake="0.5")        # never marked
    _whale_row(store, "V2/kelly", 1, "pm:a", stake="0.25")      # never marked
    _whale_row(store, "V2/flat", 2, "pm:b")                     # never marked
    _mark_bet(store, a_flat, "0.52")

    cell = markout_stats(store)["sports"][H_2M]

    assert cell["coverage"] == Decimal("0.500000")              # was 0.250000


def test_coverage_denominator_counts_a_legacy_signal_less_bet_once(cfg, tmp_path):
    """A legacy row (signal_id NULL, pre-`variant`-column data) has no sibling
    sizing rows to collapse into — it must count once in the denominator too,
    same as it always did, even mixed with a signal that dedupes three rows
    to one."""
    from arb.whale_metrics import H_2M, markout_stats
    store = Store(tmp_path / "t.sqlite")
    legacy = _whale_row(store, "V2/flat", None, "pm:a")
    flat = _whale_row(store, "V2/flat", 1, "pm:b")
    conf = _whale_row(store, "V2/conf", 1, "pm:b", stake="0.5")
    kelly = _whale_row(store, "V2/kelly", 1, "pm:b", stake="0.25")
    for bet_id in (legacy, flat, conf, kelly):
        _mark_bet(store, bet_id, "0.52")

    cell = markout_stats(store)["sports"][H_2M]

    assert cell["coverage"] == Decimal("1.000000")              # was 0.500000


def test_roi_rows_stays_one_row_per_sizing_never_deduped(cfg, tmp_path):
    """The sizing comparison (design §8, "three measurements of one
    population") needs ROI per BOOKING, not per signal — unlike the markout
    dedup above, `roi_rows`/`roi_cell` must keep all three sizing rows of one
    fired signal as three separate observations."""
    from arb.whale_metrics import roi_rows
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a", pnl=Decimal("0.10"))
    _whale_row(store, "V2/conf", 1, "pm:a", pnl=Decimal("0.20"), stake="0.5")
    _whale_row(store, "V2/kelly", 1, "pm:a", pnl=Decimal("0.05"), stake="0.25")

    rows = roi_rows(store)

    assert len(rows) == 3
    assert {r["variant"] for r in rows} == {"V2/flat", "V2/conf", "V2/kelly"}
    assert all(r["signal_id"] == 1 for r in rows)


# ------------------------------------------------------------ roots (C1)

def test_settled_rows_from_two_roots_are_never_pooled(cfg, tmp_path):
    """PREREGISTRATION 2026-09-04 (non-sports roots): "rows from different
    roots are never pooled". The ROI table is keyed (root, variant), sports
    first, and one root's bets never enter another's cell."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    for i in range(4):
        _whale_row(store, "V2/flat", i + 1, f"pm:s{i}", pnl=Decimal("0.20"))
        _whale_row(store, "V2/flat", 100 + i, f"pm:p{i}", pnl=Decimal("-0.30"),
                   category="politics")

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert list(frame["roi"]) == ["sports", "politics"]          # sports first
    assert frame["roi"]["sports"]["V2/flat"]["n"] == 4
    assert frame["roi"]["sports"]["V2/flat"]["mean"] == Decimal("0.200000")
    assert frame["roi"]["politics"]["V2/flat"]["n"] == 4
    assert frame["roi"]["politics"]["V2/flat"]["mean"] == Decimal("-0.300000")
    assert frame["bets"] == {"sports": {"V2/flat": 4}, "politics": {"V2/flat": 4}}
    md = "\n".join(report_whale.whale_md(frame))
    assert md.index("### sports root") < md.index("### politics root")


def test_the_sports_primary_metric_is_bit_for_bit_unchanged_by_a_politics_row(
        cfg, tmp_path):
    """The registration keeps the primary metric on the SPORTS root, so adding
    the first non-sports settled bet must not move a single digit of it."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    for i in range(6):
        _whale_row(store, "V2/flat", i + 1, f"pm:s{i}", pnl=Decimal(str(0.1 * i - 0.2)))
    before = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    _whale_row(store, "V2/flat", 99, "pm:p0", pnl=Decimal("5"), category="politics")
    after = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert after["roi"]["sports"] == before["roi"]["sports"]
    assert after["markouts"].get("sports") == before["markouts"].get("sports")
    assert after["kill"]["rules"] == before["kill"]["rules"]
    # and the printed sports block reproduces its own bytes
    assert report_whale._root_md(after, "sports") == report_whale._root_md(before, "sports")


def test_the_primary_kill_decision_counts_the_sports_root_only(cfg, tmp_path):
    """§12 amended: `kill_status`' four rules decide on the sports root; each
    non-sports root accumulates its own n beside them and never in them."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    for i in range(3):
        _whale_row(store, "V2/flat", i + 1, f"pm:s{i}", pnl=Decimal("0.20"))
    for i in range(5):
        _whale_row(store, "V2/flat", 100 + i, f"pm:p{i}", pnl=Decimal("-0.30"),
                   category="politics")

    kill = report_whale.whale_frame(cfg, store, now=NOW, con=con)["kill"]

    assert kill["root"] == "sports"
    assert kill["rules"][0]["n"] == 3                     # rule 1: sports only
    assert kill["rules"][3]["fired_7d"] == 3              # rule 4: sports only
    assert kill["roots"]["politics"]["n"] == 5
    assert kill["roots"]["politics"]["ub90"] == Decimal("-0.300000")
    assert kill["roots"]["politics"]["streak"] == 0
    assert "sports" not in kill["roots"]


def test_markouts_and_exits_are_reported_per_root(cfg, tmp_path):
    """§7 item 5: "the +2m/+10m markout is reported per root". Coverage is a
    root's own marks over a root's own bets, never a pooled denominator."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    s = _whale_row(store, "V2/flat", 1, "pm:s0", pnl=Decimal("0.10"))
    p = _whale_row(store, "V2/flat", 2, "pm:p0", pnl=Decimal("-0.10"),
                   category="politics")
    for bet_id, cost in ((s, "0.52"), (p, "0.60")):        # mid0 = 0.50
        store.insert("markouts", {"bet_id": bet_id, "leg_index": 0,
                                  "horizon_secs": 120, "actual_offset_secs": 120.0,
                                  "mark_cost": cost, "marked_at": NOW.isoformat(),
                                  "in_play": 0})

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert frame["markouts"]["sports"][120]["mean_drift"] == Decimal("0.020000")
    assert frame["markouts"]["politics"][120]["mean_drift"] == Decimal("0.100000")
    assert frame["markouts"]["sports"][120]["coverage"] == Decimal("1.000000")
    assert frame["exits"]["sports"]["flip"]["n"] == 1
    assert frame["exits"]["politics"]["flip"]["n"] == 1


# -------------------------------------------------------- collector health

def test_collector_health_reports_the_last_cycle_the_gap_and_the_backoff(cfg, tmp_path):
    """The operator-facing half of §12: is the tape being written at all?"""
    from arb.whale_tape import _set_meta
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    _set_meta(con, "last_cycle", json.dumps({"status": "ok", "prints": 41,
                                             "bad_rows": 2}))
    _set_meta(con, "last_seen_ts", int((NOW - timedelta(seconds=90)).timestamp()))
    _set_meta(con, "last_gap", json.dumps({"secs": 300, "at": NOW.isoformat()}))
    _set_meta(con, "backoff_until:collector", (NOW + timedelta(seconds=60)).isoformat())
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


def test_collector_health_surfaces_every_backoff_family(cfg, tmp_path):
    """DEFECT (live, 2026-09-05): backoff used to be ONE shared key, so a
    backfill 429 read, in the report, as the print collector itself being
    down. `backoff_until` stays the collector's own pause (back-compat for
    every existing reader of that field); `backoff` surfaces all three
    families independently so a reader can tell them apart."""
    from arb.whale_tape import _set_meta
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    _set_meta(con, "backoff_until:backfill", (NOW + timedelta(seconds=60)).isoformat())
    _set_meta(con, "backoff_until:resolve", (NOW + timedelta(seconds=120)).isoformat())
    con.commit()

    c = report_whale.whale_frame(cfg, store, now=NOW, con=con)["collector"]

    assert c["backoff_until"] is None                 # collector itself is fine
    assert c["backoff"]["collector"] is None
    assert c["backoff"]["backfill"] is not None
    assert c["backoff"]["resolve"] is not None


def test_prints_24h_uses_a_true_24h_window_not_a_calendar_day_string_compare(cfg, tmp_path):
    """DEFECT hypothesis (live, 2026-09-05): SQLite's `datetime('now','-1
    day')` is a SPACE-separated string ('2026-09-04 02:00:00'); `observed_at`
    is T-separated ISO ('2026-09-04T...'). Compared lexicographically, 'T'
    (0x54) sorts above ' ' (0x20), so a row would pass `observed_at >=
    datetime('now','-1 day')` for its whole calendar day regardless of time —
    up to 48h read as "24h". `_collector` already avoids this (a Python
    cutoff from `now - timedelta(...)`, bound as a query parameter, same ISO
    shape as the stored value); this locks it in with a row 25.5h old that
    the string-compare bug would have wrongly kept."""
    now = datetime(2026, 9, 5, 2, 0, 0, tzinfo=timezone.utc)
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    # 25.5h before `now`: truly outside the 24h window, but dated the same
    # calendar day as the buggy cutoff's date — the string-compare bug would
    # have kept it (it only compares '2026-09-04T...' >= '2026-09-04 02:00:00').
    outside = "2026-09-04T00:30:00+00:00"
    # 6h before `now`: genuinely inside the window either way.
    inside = "2026-09-04T20:00:00+00:00"
    for i, at in enumerate((outside, inside)):
        con.execute("INSERT INTO prints (observed_at, ts, proxy_wallet, condition_id, "
                    "asset, outcome_index, side, price, size, notional_usd) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (at, i, f"w{i}", CID, "0xyes", 0, "BUY", "0.50", "1000", "500"))
        con.execute("INSERT INTO holdings (observed_at, proxy_wallet, condition_id, "
                    "asset, outcome_index, size) VALUES (?,?,?,?,?,?)",
                    (at, f"w{i}", CID, "0xyes", 0, "1"))
    con.commit()

    c = report_whale.whale_frame(cfg, store, now=now, con=con)["collector"]

    assert c["prints_24h"] == 1            # only `inside`
    assert c["holdings_24h"] == 1


def test_the_ops_line_watches_the_tape_size_and_the_rows_per_day(cfg, tmp_path):
    """Proposal §2 implementation notes (retention amendment 2026-09-04): the
    17 GB/year is watched, not discovered. prints/day and holdings rows/day are
    the 24h before `now` by `observed_at` — a function of the report day, so
    they print in the hashed md. The file size (a stat, never an open) is a
    LIVE fact: frame and console line only, never the md
    (tests/test_report.py::test_write_daily_hash_ignores_collector_churn_and_future_tape_rows).
    bytes_per_day is a trailing-24h ESTIMATE (2026-09-05 fix — a lifetime
    average used to stay wrong for a full day after any compaction): rows
    written across prints/holdings/signals in the last 24h times the tape's
    mean bytes/row over ALL rows of those tables."""
    import os
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    # oldest first: observed_at is the collector's clock at insert, so the
    # tape's rowid order IS its observed_at order (what bytes/day dates by)
    for age_h in (25, 23, 1):                  # one before the day, two inside
        at = NOW - timedelta(hours=age_h)
        con.execute("INSERT INTO prints (observed_at, ts, proxy_wallet, condition_id, "
                    "asset, outcome_index, side, price, size, notional_usd) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (at.isoformat(), int(at.timestamp()), "w1", CID, "0xyes", 0,
                     "BUY", "0.50", "1000", "500"))
    for age_h in (30, 4, 3, 2):                # one before the day, three inside
        con.execute("INSERT INTO holdings (observed_at, proxy_wallet, condition_id, "
                    "asset, outcome_index, size) VALUES (?,?,?,?,?,?)",
                    ((NOW - timedelta(hours=age_h)).isoformat(), "w1", CID,
                     "0xyes", 0, "1"))
    con.commit()
    size = os.path.getsize(tmp_path / "tape.sqlite")

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    c = frame["collector"]
    assert c["prints_24h"] == 2 and c["holdings_24h"] == 3
    assert c["tape_bytes"] == size
    # 5 rows in the trailing 24h (2 prints + 3 holdings + 0 signals) out of 7
    # total (3 prints + 4 holdings + 0 signals) across the tape's lifetime.
    assert c["bytes_per_day"] == int(Decimal(size) * 5 / 7)
    md = "\n".join(report_whale.whale_md(frame))
    assert "- tape: 2 prints/day, 3 holdings rows/day (24h to as-of)" in md
    mb, mb_day = (f"{Decimal(b) / 1000000:.1f}" for b in (size, c["bytes_per_day"]))
    assert f"{mb} MB" not in md                # live: never in the hashed md
    assert (f"tape {mb} MB, ~{mb_day} MB/day (est. trailing 24h)"
            in report_whale.whale_line(frame))


def test_bytes_per_day_scales_up_for_a_tape_younger_than_24h(cfg, tmp_path):
    """Review follow-up (2026-09-05): when the tape is younger than 24h every
    row falls in the trailing-24h window, so rows_24h == total_rows and the
    raw estimate collapses to tape_bytes itself — understating a young tape's
    true daily rate. Scale by 86400/age_secs off the oldest print's age and
    flag the estimate low-confidence."""
    import os
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    for age_h in (6, 3, 1):                        # oldest print is 6h old
        at = NOW - timedelta(hours=age_h)
        con.execute("INSERT INTO prints (observed_at, ts, proxy_wallet, condition_id, "
                    "asset, outcome_index, side, price, size, notional_usd) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (at.isoformat(), int(at.timestamp()), "w1", CID, "0xyes", 0,
                     "BUY", "0.50", "1000", "500"))
    con.commit()
    size = os.path.getsize(tmp_path / "tape.sqlite")

    c = report_whale.whale_frame(cfg, store, now=NOW, con=con)["collector"]

    # all 3 rows are within the trailing 24h AND the tape's whole lifetime, so
    # the raw estimate is tape_bytes * 3/3 == tape_bytes; scaled 86400/21600 == 4x
    assert c["bytes_per_day"] == int(Decimal(size) * 4)
    assert c["bytes_per_day_confidence"] == "low"


def test_bytes_per_day_confidence_is_ok_for_a_tape_older_than_24h(cfg, tmp_path):
    """The existing (>24h) tape from test_the_ops_line_watches_the_tape_size_
    and_the_rows_per_day gets no scaling applied to its estimate."""
    import os
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    for age_h in (25, 23, 1):
        at = NOW - timedelta(hours=age_h)
        con.execute("INSERT INTO prints (observed_at, ts, proxy_wallet, condition_id, "
                    "asset, outcome_index, side, price, size, notional_usd) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (at.isoformat(), int(at.timestamp()), "w1", CID, "0xyes", 0,
                     "BUY", "0.50", "1000", "500"))
    con.commit()

    c = report_whale.whale_frame(cfg, store, now=NOW, con=con)["collector"]

    assert c["bytes_per_day_confidence"] == "ok"


def test_the_ops_line_watches_the_position_cache(cfg, tmp_path):
    """matpos (2026-09-05): the scoring pass reads a per-wallet cache the lane
    keeps current, so the ops line has to say whether it IS current — how many
    positions it holds, how many wallets are still queued, what the last pass
    cost. A backlog that does not fall cycle to cycle is the alarm. LIVE, off
    the lane's own last cycle, so never in the hashed md."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    con.execute("INSERT INTO meta (key, value) VALUES ('last_cycle', ?)",
                (json.dumps({"status": "ok", "prints": 12, "at": NOW.isoformat(),
                             "mat": {"wallets": 400, "dirty": 2600, "rows": 57290,
                                     "secs": 8.78}}),))
    con.commit()

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert frame["collector"]["mat"]["dirty"] == 2600
    line = report_whale.whale_line(frame)
    assert "positions 57290, 2600 dirty, 8.78s" in line
    assert "57290" not in "\n".join(report_whale.whale_md(frame))


def test_the_ops_line_survives_a_cycle_that_predates_the_position_cache(cfg, tmp_path):
    """The first report after this ships reads a `last_cycle` written by the
    old lane, which has no `mat` key at all — the line drops the figure rather
    than raising."""
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    con.execute("INSERT INTO meta (key, value) VALUES ('last_cycle', ?)",
                (json.dumps({"status": "ok", "prints": 12, "at": NOW.isoformat()}),))
    con.commit()

    line = report_whale.whale_line(report_whale.whale_frame(cfg, store, now=NOW, con=con))

    assert "dirty" not in line and "collector ok" in line


def test_the_ops_line_reads_the_tape_size_without_a_readable_tape(cfg, tmp_path):
    """A WAL tape under its writer can refuse a read-only open (open_tape_ro ->
    None) and is still on disk, growing — the one case the size matters most."""
    cfg["whale"]["tape_db"] = str(tmp_path / "locked.sqlite")
    (tmp_path / "locked.sqlite").write_bytes(b"x" * 4096)
    store = Store(tmp_path / "t.sqlite")

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=None)

    assert frame["tape_ok"] is False
    assert frame["collector"]["tape_bytes"] == 4096
    assert frame["collector"]["bytes_per_day"] is None       # no first print to date it
    assert "tape 0.0 MB, ~— MB/day" in report_whale.whale_line(frame)


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


def test_the_report_prints_the_v5_elite_bar_and_its_population(cfg, tmp_path):
    """§3's stated instability: the elite set moves week to week, so the daily
    report prints the per-bucket cutoff and the population size behind it —
    a step change in either is attributable, not inferred. A cutoff at or below
    `min_score_lb` is called out: V5 is then a copy of V1."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    cfg["whale"]["min_score_lb"] = Decimal("0.0")            # the shipped bar
    for bucket, cutoff, at in (("baseball_mlb:WINNER", "0.0118", NOW - timedelta(hours=2)),
                               ("baseball_mlb:WINNER", "0.0118", NOW),
                               ("baseball_mlb:TOTAL", "-0.0046", NOW)):
        con.execute("INSERT INTO elite_cutoffs (root, bucket, as_of, cutoff, n)"
                    " VALUES ('sports',?,?,?,170)", (bucket, at.isoformat(), cutoff))
    con.commit()

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)
    md = "\n".join(report_whale.whale_md(frame))

    assert frame["elite"]["pct"] == 20
    assert [r["bucket"] for r in frame["elite"]["cutoffs"]] == [
        "baseball_mlb:TOTAL", "baseball_mlb:WINNER"]        # newest per bucket
    assert "top 20%" in md
    assert "baseball_mlb:WINNER 0.0118 (pop 170)" in md
    assert "copy of V1" in md                                # the degenerate bucket


def test_the_report_never_prints_a_cutoff_or_base_rate_from_after_now(cfg, tmp_path):
    """Coordinator review of a42fb14, rule 9 determinism: `whale_elite.latest`
    and the report's own `base_rates` read both picked the globally newest row
    (`MAX(as_of)`) with no bound to the report's `now` — so re-running `arb
    report --daily` for a past day printed today's cutoff/base rate instead of
    the one that was actually in force on the day being reported. The existing
    elite-bar test above only ever inserts rows at or before `now`, so it
    cannot catch a later row leaking backward; this one plants one after `now`
    for both tables and asserts the report still shows the earlier reading."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    future = NOW + timedelta(days=1)
    con.execute("INSERT INTO elite_cutoffs (root, bucket, as_of, cutoff, n)"
                " VALUES ('sports', 'baseball_mlb:WINNER', ?, '0.0118', 170)",
                (NOW.isoformat(),))
    con.execute("INSERT INTO elite_cutoffs (root, bucket, as_of, cutoff, n)"
                " VALUES ('sports', 'baseball_mlb:WINNER', ?, '0.9999', 999)",
                (future.isoformat(),))
    con.execute("INSERT INTO base_rates (root, as_of, value, n) VALUES ('sports', ?, '0.0428', 6713)",
                (NOW.isoformat(),))
    con.execute("INSERT INTO base_rates (root, as_of, value, n) VALUES ('sports', ?, '0.9999', 1)",
                (future.isoformat(),))
    con.commit()

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert [dict(c) for c in frame["elite"]["cutoffs"]] == [
        {"root": "sports", "bucket": "baseball_mlb:WINNER", "as_of": NOW.isoformat(),
         "cutoff": "0.0118", "n": 170}]
    assert [dict(b) for b in frame["base_rates"]] == [
        {"root": "sports", "as_of": NOW.isoformat(), "value": "0.0428", "n": 6713}]


def test_the_report_writes_nothing_to_the_tape(cfg, tmp_path):
    """Rule 9's spirit: the section is a read. Base rates are the LANE's write."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    before = con.execute("SELECT COUNT(*) c FROM base_rates").fetchone()["c"]
    changes_before = con.total_changes

    report_whale.whale_frame(cfg, store, now=NOW + timedelta(hours=1), con=con)

    assert con.execute("SELECT COUNT(*) c FROM base_rates").fetchone()["c"] == before
    assert con.total_changes == changes_before


# ------------------------------------------------- §12 kill rules (Task 12)

def _mark(store, bet_id, mark_cost="0.40", horizon=None, in_play=0, marked_at=NOW):
    from arb.whale_metrics import H_2M
    return store.insert("markouts", {
        "bet_id": bet_id, "leg_index": 0, "horizon_secs": horizon or H_2M,
        "actual_offset_secs": 121.0, "mark_cost": mark_cost,
        "marked_at": marked_at.isoformat(), "in_play": in_play})


def _hashes(tmp_path, *days):
    """`write_daily`'s append-only chain: one line per PRODUCED daily report,
    which is the only record rule 2's streak is counted from."""
    path = tmp_path / "HASHES.log"
    path.write_text("".join(f"{d} md:deadbeef csv:deadbeef\n" for d in days))
    return path


def _rules(cfg, store, con=None, hashes=None, producing_day=None):
    from arb.whale_kill import kill_status
    # no chain = no report was ever produced here; the rule-2 tests bring theirs
    return {r["rule"]: r for r in kill_status(
        cfg, store, con, NOW, hashes or "/nonexistent/HASHES.log",
        producing_day=producing_day)["rules"]}


def test_every_kill_rule_starts_at_not_yet_on_an_empty_ledger(cfg, tmp_path):
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)

    rules = _rules(cfg, store, con)

    assert [r["state"] for r in rules.values()] == ["not yet"] * 4
    assert rules[1]["n"] == 0 and rules[1]["target_n"] == 100
    assert rules[3]["target_n"] == 1200
    assert rules[4]["collecting_days"] == 0


def test_rule_1_fires_when_the_upper_bound_is_negative_at_decision_n(cfg, tmp_path):
    """§12 rule 1: at n >= decision_n, ub90 < 0 kills. n counts DISTINCT
    signal_ids of the primary variant, and the bound is the one-sided 90th."""
    cfg["whale"]["kill"]["decision_n"] = 3
    store = Store(tmp_path / "t.sqlite")
    for sid in range(1, 4):
        _whale_row(store, "V2/flat", sid, f"pm:{sid}", pnl="-0.20")
    _whale_row(store, "V2/conf", 9, "pm:9", pnl="0.90")     # other variant: ignored

    rules = _rules(cfg, store)

    assert rules[1]["n"] == 3 and rules[1]["ub90"] < 0
    assert rules[1]["state"] == "FIRED"
    assert "n=3/3" in rules[1]["says"]


def test_rule_1_is_armed_but_unfired_while_the_bound_straddles_zero(cfg, tmp_path):
    cfg["whale"]["kill"]["decision_n"] = 2
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:1", pnl="0.50")
    _whale_row(store, "V2/flat", 2, "pm:2", pnl="-0.40")

    rules = _rules(cfg, store)

    assert rules[1]["state"] == "armed" and rules[1]["ub90"] > 0


def test_rule_2_needs_two_consecutive_produced_reports(cfg, tmp_path):
    """Drift of -0.10 against a flagged_edge of 0.05 clears -0.5 x edge, so the
    condition holds; one produced report is still not two."""
    cfg["whale"]["kill"]["fast_kill_min_marked"] = 1
    store = Store(tmp_path / "t.sqlite")
    yesterday = NOW - timedelta(days=1)
    _mark(store, _whale_row(store, "V2/flat", 1, "pm:1", created=yesterday),
          marked_at=yesterday)

    one = _rules(cfg, store, hashes=_hashes(tmp_path, DAY))[2]
    two = _rules(cfg, store, hashes=_hashes(tmp_path, "2026-09-02", DAY))[2]

    assert one["mean_d2m"] == Decimal("-0.100000")
    assert one["threshold"] == Decimal("-0.025000")
    assert one["held_today"] is True
    assert one["streak"] == 1 and one["state"] == "armed"
    assert two["streak"] == 2 and two["state"] == "FIRED"


def test_the_day_being_produced_counts_toward_its_own_streak(cfg, tmp_path):
    """cmd_report scores whale_frame BEFORE write_daily appends the day's own
    HASHES.log line, so without `producing_day` the printed report undercounts
    its own streak by one: held on D-1 and D, but only D-1 is in the chain
    yet, the D report must still see streak 2 — matching what the dashboard
    (which passes `now`'s day) already shows live. Re-running D once its own
    line exists changes nothing (rule 9: recomputed from the raw log)."""
    cfg["whale"]["kill"]["fast_kill_min_marked"] = 1
    store = Store(tmp_path / "t.sqlite")
    yesterday = NOW - timedelta(days=1)
    _mark(store, _whale_row(store, "V2/flat", 1, "pm:1", created=yesterday),
          marked_at=yesterday)
    log = _hashes(tmp_path, "2026-09-02")          # D-1 produced; D not written yet

    producing = _rules(cfg, store, hashes=log, producing_day=DAY)[2]
    not_producing = _rules(cfg, store, hashes=log)[2]

    assert producing["streak"] == 2 and producing["state"] == "FIRED"
    assert not_producing["streak"] == 1 and not_producing["state"] == "armed"

    log_after = _hashes(tmp_path, "2026-09-02", DAY)   # D's own line lands
    rerun = _rules(cfg, store, hashes=log_after, producing_day=DAY)[2]
    assert rerun["streak"] == 2


def test_rule_2_stays_not_yet_below_the_marked_bet_floor(cfg, tmp_path):
    cfg["whale"]["kill"]["fast_kill_min_marked"] = 50
    store = Store(tmp_path / "t.sqlite")
    _mark(store, _whale_row(store, "V2/flat", 1, "pm:1"))

    rule = _rules(cfg, store)[2]

    assert rule["n_marked"] == 1 and rule["held_today"] is False
    assert rule["state"] == "not yet"


def test_the_streak_stops_at_the_first_produced_day_that_was_clear(cfg, tmp_path):
    """The condition is re-evaluated AS OF each produced day's end, not with
    today's window dated backwards: yesterday's booking did not exist the day
    before, so that report was clear and the run stops there."""
    from arb.whale_kill import fast_kill_streak
    cfg["whale"]["kill"]["fast_kill_min_marked"] = 1
    store = Store(tmp_path / "t.sqlite")
    yesterday = NOW - timedelta(days=1)
    _mark(store, _whale_row(store, "V2/flat", 1, "pm:1", created=yesterday),
          marked_at=yesterday)
    log = _hashes(tmp_path, "2026-08-31", "2026-09-01", "2026-09-02", DAY)

    assert fast_kill_streak(cfg, store, log, NOW) == {
        "streak": 2, "last_produced": DAY}


def test_an_outage_day_pauses_the_streak_instead_of_resetting_it(cfg, tmp_path):
    """§12: a day with no report is not counted AND does not break the run —
    the streak counts produced reports, and an outage produced none."""
    from arb.whale_kill import fast_kill_streak
    cfg["whale"]["kill"]["fast_kill_min_marked"] = 1
    store = Store(tmp_path / "t.sqlite")
    booked = NOW - timedelta(days=2)
    _mark(store, _whale_row(store, "V2/flat", 1, "pm:1", created=booked),
          marked_at=booked)
    log = _hashes(tmp_path, "2026-08-31", "2026-09-01", DAY)   # 09-02: outage

    assert fast_kill_streak(cfg, store, log, NOW)["streak"] == 2


def test_re_running_a_days_report_changes_neither_the_streak_nor_the_ledger(
        cfg, tmp_path):
    """Rule 9's prose — the report is recomputed from the raw log. The verdict
    is derived from the hash chain at read time, so regenerating a day writes
    nothing to the ledger and the count is unchanged."""
    cfg["whale"]["kill"]["fast_kill_min_marked"] = 1
    store, out = Store(tmp_path / "t.sqlite"), tmp_path / "out"
    yesterday = NOW - timedelta(days=1)
    _mark(store, _whale_row(store, "V2/flat", 1, "pm:1", created=yesterday),
          marked_at=yesterday)
    write_daily(cfg, store, DAY, outdir=out)

    changes = store.conn.total_changes
    first = report_whale.whale_frame(cfg, store, now=NOW,
                                     hashes_log_path=out / "HASHES.log")
    for _ in range(3):
        write_daily(cfg, store, DAY, outdir=out)
    again = report_whale.whale_frame(cfg, store, now=NOW,
                                     hashes_log_path=out / "HASHES.log")

    assert store.conn.total_changes == changes           # the report is a READ
    assert store.query("SELECT * FROM annotations") == []
    rule = {r["rule"]: r for r in again["kill"]["rules"]}[2]
    assert rule["streak"] == 1
    assert rule["streak"] == {r["rule"]: r for r in first["kill"]["rules"]}[2]["streak"]
    assert len((out / "HASHES.log").read_text().splitlines()) == 4


def test_rule_4_arms_only_after_four_weeks_of_collection(cfg, tmp_path):
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)
    con.execute("INSERT INTO prints (observed_at, ts, proxy_wallet, condition_id) "
                "VALUES (?,?,?,?)",
                ((NOW - timedelta(days=29)).isoformat(),
                 int((NOW - timedelta(days=29)).timestamp()), "w1", CID))
    con.commit()

    rule = _rules(cfg, store, con)[4]

    assert rule["collecting_days"] == 29 and rule["fired_7d"] == 0
    assert rule["state"] == "FIRED"        # armed AND starved: no fired signal


def test_the_whale_section_prints_the_kill_block(cfg, tmp_path):
    store, con = Store(tmp_path / "t.sqlite"), _tape(tmp_path)

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)
    md = "\n".join(report_whale.whale_md(frame))

    assert "| kill rule (§12) | state | numbers |" in md
    for name in ("1 decision", "2 fast kill", "3 null", "4 starvation"):
        assert f"| {name} | not yet |" in md


# ---------------------------------------- `whale-score --calibrate` (I4)

def _calibration_tape(con, n_whales, lb="0.05", notional="500"):
    """`n_whales` distinct wallets, each qualifying in its own evaluated
    signal — the rows §7.2's calibration reads."""
    for i in range(n_whales):
        _signal_row(con, i + 1, [{"wallet": f"0xw{i}", "side": 0, "lb": lb,
                                  "open_notional": notional, "native": True,
                                  "n_bucket": 4, "sybil_dropped": False}])


def test_whale_score_calibrate_prints_the_yaml_to_paste(cfg, tmp_path, capsys):
    """I4: §7.2 says V2/V3 stay null until a calibration run sets them, and
    nothing could run it. The command prints; the amendment stays a human
    commit (rule 4 — it never edits config.yaml itself)."""
    from arb.commands import cmd_whale_score
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    cfg["whale"]["min_score_lb"] = Decimal("0.0")     # the shipped bar: excess = lb
    con = _tape(tmp_path)
    _calibration_tape(con, int(cfg["whale"]["fire"]["calibration_min_whales"]))

    cmd_whale_score(cfg, Store(tmp_path / "arb.sqlite"),
                    type("A", (), {"calibrate": True})())

    out = capsys.readouterr().out
    assert "V2: 0.150000" in out and "V3: " in out
    assert "min_points:" in out                     # the YAML lines to paste
    assert "V4: 3" in out                # the literal thresholds come along, or
    assert "V6: 0.0008" in out           # pasting the line drops a required key
    assert "V6 =" not in out             # ... but V6 is never CALIBRATED: its
                                         # threshold is a measured literal (§7)

    # and the CLI knows the subcommand + flag (--help exits before any DB open)
    from arb import cli
    with pytest.raises(SystemExit):
        cli.main(["whale-score", "--help"])
    assert "--calibrate" in capsys.readouterr().out


def test_whale_score_calibrate_says_how_far_short_the_window_is(cfg, tmp_path, capsys):
    from arb.commands import cmd_whale_score
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    con = _tape(tmp_path)
    _calibration_tape(con, 3)

    cmd_whale_score(cfg, Store(tmp_path / "arb.sqlite"),
                    type("A", (), {"calibrate": True})())

    out = capsys.readouterr().out
    assert "None" in out and "3" in out and "10" in out


# ---------------------------------- `whale-score` without `--calibrate` (I2)

def test_whale_score_without_calibrate_never_creates_the_tape(cfg, tmp_path, capsys):
    """The subcommand's own help text says read-only, but `whale_tape.open_tape`
    creates the file on first touch — a bare `whale-score` used to leave a tape
    database behind that the collector had never written. `open_tape_ro`
    reports the tape unavailable instead of creating it."""
    from arb.commands import cmd_whale_score
    tape_path = tmp_path / "tape.sqlite"
    cfg["whale"]["tape_db"] = str(tape_path)

    cmd_whale_score(cfg, Store(tmp_path / "arb.sqlite"),
                    type("A", (), {"calibrate": False})())

    assert not tape_path.exists()
    assert "unavailable" in capsys.readouterr().out


def test_whale_score_without_calibrate_prints_the_roster(cfg, tmp_path, capsys):
    """Without `--calibrate`: the lane's own qualifying-whale roster (same
    scoring assembly the daily report reads, arb/whale_state.py), never the
    calibration YAML."""
    from arb.commands import cmd_whale_score
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)                          # books the cycle's three whales

    cmd_whale_score(cfg, store, type("A", (), {"calibrate": False})())

    out = capsys.readouterr().out
    assert "roster: 3 whale" in out and "sports 3" in out
    assert "min_points" not in out                  # never the calibration branch


# ------------------------------------------- per-cycle whole-tape work (I5)

def test_the_materialized_print_read_takes_only_the_columns_it_uses(cfg, tmp_path):
    """I5(a): `SELECT *` pulled `raw_json` — the whole venue payload of every
    print ever collected — into memory on every 60 s cycle. matpos
    (2026-09-05) moved that read out of `score_state`, which now touches no
    print at all (tests/test_whale_mat.py), and into the per-wallet
    materialization — where the same column discipline still binds, because
    the backfill child re-materializes thousands of wallets an hour."""
    from arb import whale_mat
    con = _tape(tmp_path)
    con.execute(
        "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, condition_id,"
        " asset, outcome_index, side, price, size, notional_usd, event_slug, source,"
        " raw_json) VALUES (?,?,?,?,?,?,0,'BUY',?,?,?,?,'tape',?)",
        (NOW.isoformat(), int(NOW.timestamp()), "0xt", "0xw", CID, "tok",
         "0.500000", "1000.000000", "500.000000", "e", "x" * 10000))
    con.commit()

    seen = []
    con.set_trace_callback(seen.append)
    try:
        whale_mat.materialize(con, cfg, NOW)
    finally:
        con.set_trace_callback(None)

    assert not [q for q in seen if "raw_json" in q], seen
    assert {"ts", "proxy_wallet", "condition_id", "outcome_index", "side", "price",
            "size", "notional_usd", "source", "asset",
            "event_slug"} == set(whale_mat._PRINT_FIELDS)


def test_score_state_query_constants_select_no_raw_json_and_no_star():
    """Retention regrade 2026-09-04 (score_state read trim, part 2): the
    `markets`/`resolutions` reads were still `SELECT *`, pulling `raw_json` —
    the resolution's whole venue payload — and columns state.markets/
    state.resolutions never serve a reader (first_seen/last_seen/volume_24h).
    Introspects the SQL constants directly, so a future `SELECT *` regression
    fails here instead of only showing up as a slower cycle."""
    from arb import whale_state
    for name in ("PRINT_COLS", "MARKET_COLS", "RESOLUTION_COLS"):
        cols = getattr(whale_state, name)
        assert "raw_json" not in cols, name
        assert "*" not in cols, name


def test_score_state_reads_only_the_market_and_resolution_columns_it_uses(cfg, tmp_path):
    """The other half of the trim: `state.markets[cid]` still carries every
    field a reader off it needs — positions_from_prints (sport, market_type,
    start_utc), the lane's `_tape_market`/`_event_key` for an aged-out held
    market (line, event_key, tokens_json, slug) — but not the columns nothing
    reads back through `state`. `state.resolutions[cid]` keeps what
    resolved_outcomes/whale_rank read (resolved_at, winner_outcome_index,
    status) and drops the venue's raw resolution payload."""
    from arb.whale_state import score_state
    con = _tape(tmp_path)
    con.execute(
        "INSERT INTO markets (condition_id, sport, market_type, line, start_utc,"
        " event_key, tokens_json, slug, first_seen, last_seen, volume_24h)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (CID, "baseball_mlb", "WINNER", None, NOW.isoformat(), "pm:evt",
         '[["0xyes","Yes"]]', "slug-x", NOW.isoformat(), NOW.isoformat(), "1000"))
    con.execute(
        "INSERT INTO resolutions (condition_id, resolved_at, winner_outcome_index,"
        " status, source, raw_json, fetched_at) VALUES (?,?,0,'resolved','gamma',?,?)",
        (CID, NOW.isoformat(), "x" * 10000, NOW.isoformat()))
    con.commit()

    state = score_state(cfg, con, NOW)

    mk = state.markets[CID]
    assert {"condition_id", "sport", "market_type", "line", "start_utc",
            "event_key", "tokens_json", "slug"} <= set(mk)
    assert not {"first_seen", "last_seen", "volume_24h"} & set(mk)

    res = state.resolutions[CID]
    assert {"condition_id", "resolved_at", "winner_outcome_index", "status"} <= set(res)
    assert not {"source", "raw_json", "fetched_at"} & set(res)


def test_score_state_matches_the_select_star_reference_on_a_mixed_tape(cfg, tmp_path):
    """Equivalence: the column trim changes what state.markets/state.resolutions
    dicts CARRY, never what score_state COMPUTES off them. `_reference` below
    is score_state's body frozen at `SELECT * FROM markets`/`SELECT * FROM
    resolutions` (pre-trim); the tape mixes a resolved clean position with a
    wash-flagged (hygiene-excluded) one, so positions, open_positions,
    hygiene_excluded, agg and a scored lookup all have something to disagree
    on if the trim ever drops a column a reader actually needs."""
    from arb import whale_bands, whale_mat, whale_score
    from arb.whale_hygiene import hygiene_flags
    from arb.whale_state import WhaleState, score_state

    def _reference(cfg, con, now):
        prints = [dict(r) for r in con.execute("SELECT * FROM prints")]
        markets = {r["condition_id"]: dict(r) for r in con.execute("SELECT * FROM markets")}
        resolutions = {r["condition_id"]: dict(r)
                       for r in con.execute("SELECT * FROM resolutions")}
        positions = whale_score.positions_from_prints(prints, markets, cfg)
        hyg = hygiene_flags(prints, positions, cfg)
        open_pos = [p for p in positions if not hyg.excludes(p)]
        outcomes = whale_score.resolved_outcomes(open_pos, resolutions, now)
        # the band grading score_state runs (amendment 2026-09-05); this
        # reference exists to pin what the ROW DICTS carry, not to re-litigate
        # the scoring chain, so it tracks that chain line for line.
        bands = whale_bands.table(outcomes, now, cfg)
        outcomes = whale_bands.adjust(outcomes, bands, cfg)
        agg = whale_score.aggregate(outcomes)
        rates, rate_n = {}, {}
        for root in {o.root for o in outcomes} | {"sports"}:
            rates[root], rate_n[root] = whale_score.base_rate(outcomes, root, now, cfg)

        def lookup(wallet, sport, market_type):
            return whale_score.score_for(agg, wallet, sport, market_type, cfg, rates)

        return WhaleState(markets=markets, resolutions=resolutions,
                          positions=positions, open_positions=open_pos,
                          hygiene_excluded=len(positions) - len(open_pos),
                          rates=rates, rate_n=rate_n, agg=agg, lookup=lookup,
                          bands=bands)

    con = _tape(tmp_path)
    resolved_at = NOW - timedelta(hours=1)
    start = NOW - timedelta(days=2)
    for cid, wallet, wash in (("0xclean", "0xw1", False), ("0xwash", "0xw2", True)):
        con.execute(
            "INSERT INTO markets (condition_id, sport, market_type, start_utc,"
            " tokens_json, first_seen, last_seen) VALUES (?,?,?,?,'[]',?,?)",
            (cid, "baseball_mlb", "WINNER", start.isoformat(),
             start.isoformat(), start.isoformat()))
        con.execute(
            "INSERT INTO resolutions (condition_id, resolved_at,"
            " winner_outcome_index, status, source, raw_json, fetched_at)"
            " VALUES (?,?,0,'resolved','gamma','{}',?)",
            (cid, resolved_at.isoformat(), resolved_at.isoformat()))
        ts = int((start - timedelta(hours=1)).timestamp())
        rows = [("BUY", ts, "0.50", "1000")]
        if wash:
            rows.append(("SELL", ts + 1, "0.50", "1"))
        for j, (side, at, px, size) in enumerate(rows):
            con.execute(
                "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet,"
                " condition_id, asset, outcome_index, side, price, size,"
                " notional_usd, source, raw_json) VALUES (?,?,?,?,?,?,0,?,?,?,?,'tape','{}')",
                (start.isoformat(), at, f"0x{wallet}{j}", wallet, cid, "tok",
                 side, px, size, str(Decimal(px) * Decimal(size))))
    con.commit()
    whale_mat.materialize(con, cfg, NOW)

    got = score_state(cfg, con, NOW)
    want = _reference(cfg, con, NOW)

    assert got.positions == want.positions
    assert got.open_positions == want.open_positions
    assert got.hygiene_excluded == want.hygiene_excluded == 1   # only the wash wallet
    assert got.agg == want.agg
    assert got.rates == want.rates and got.rate_n == want.rate_n
    for wallet in ("0xw1", "0xw2"):
        assert (got.lookup(wallet, "baseball_mlb", "WINNER")
                == want.lookup(wallet, "baseball_mlb", "WINNER"))


# ------------------------------------------------- rowperf (2026-09-05): the
# prints ROW OBJECT (not the columns — that was the 2026-09-04 pair above).
# Profiled live 2026-09-05 08:50Z on the production tape (763,786 prints):
# score_state 29.4s, 23.7s its own body, all of it `[dict(r) for r in ...]` —
# a real `dict` built and hashed, 11 keys, for every print. `_PrintRow`
# (arb/whale_state.py) wraps the row's own tuple instead — same `row["x"]`/
# `.get`/`in`/`set(row)` surface, no dict. `_reference_score_state` below is
# score_state's body frozen at `dict(r)` (post the 2026-09-04 column trim,
# pre this pass) — not imported, so this can never rot into comparing the
# real function against itself.

def _reference_score_state(cfg, con, now):
    from arb import whale_score
    from arb.whale_hygiene import hygiene_flags
    from arb.whale_state import (MARKET_COLS, PRINT_COLS, PRINT_ORDER,
                                 RESOLUTION_COLS, WhaleState)

    prints = [dict(r) for r in con.execute(f"SELECT {PRINT_COLS} FROM prints {PRINT_ORDER}")]
    markets = {r["condition_id"]: dict(r)
               for r in con.execute(f"SELECT {MARKET_COLS} FROM markets")}
    resolutions = {r["condition_id"]: dict(r)
                   for r in con.execute(f"SELECT {RESOLUTION_COLS} FROM resolutions")}
    positions = whale_score.positions_from_prints(prints, markets, cfg)
    hyg = hygiene_flags(prints, positions, cfg)
    open_pos = [p for p in positions if not hyg.excludes(p)]
    outcomes = whale_score.resolved_outcomes(open_pos, resolutions, now)
    agg = whale_score.aggregate(outcomes)
    rates, rate_n = {}, {}
    for root in {o.root for o in outcomes} | {"sports"}:
        rates[root], rate_n[root] = whale_score.base_rate(outcomes, root, now, cfg)

    def lookup(wallet, sport, market_type):
        return whale_score.score_for(agg, wallet, sport, market_type, cfg, rates)

    return WhaleState(markets=markets, resolutions=resolutions,
                      positions=positions, open_positions=open_pos,
                      hygiene_excluded=len(positions) - len(open_pos),
                      rates=rates, rate_n=rate_n, agg=agg, lookup=lookup)


def _random_score_state_tape(con, rng, n_prints, n_wallets=15, n_markets=6, now=NOW):
    """Seeded synthetic tape covering the branches score_state's callers
    care about: resolved and unresolved markets, dust prints (below
    min_print_usd=200), SELLs (FIFO), and enough wallet/market overlap for
    hygiene's mm/wash/both_sides scans to actually fire on some of them."""
    wallets = [f"0xw{i}" for i in range(n_wallets)]
    cids = [f"0xc{i}" for i in range(n_markets)]
    start = now - timedelta(days=5)
    resolved_at = now - timedelta(hours=2)
    con.executemany(
        "INSERT INTO markets (condition_id, sport, market_type, line, start_utc,"
        " event_key, tokens_json, slug, first_seen, last_seen, volume_24h)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(c, "baseball_mlb", "WINNER", None, start.isoformat(), f"pm:evt{i}",
          "[]", f"slug{i}", start.isoformat(), start.isoformat(), "1000")
         for i, c in enumerate(cids)])
    con.executemany(
        "INSERT INTO resolutions (condition_id, resolved_at, winner_outcome_index,"
        " status, source, raw_json, fetched_at) VALUES (?,?,?,'resolved','gamma','{}',?)",
        [(c, resolved_at.isoformat(), rng.choice((0, 1)), resolved_at.isoformat())
         for c in cids[:max(1, n_markets // 2)]])

    prices = ["0.05", "0.10", "0.35", "0.50", "0.65", "0.90"]
    sizes = ["5", "50", "300", "2000", "8000"]        # some clear dust (< $200), some don't
    rows = []
    for i in range(n_prints):
        w, c = rng.choice(wallets), rng.choice(cids)
        idx = rng.choice((0, 1))
        side = "SELL" if rng.random() < 0.15 else "BUY"
        price, size = rng.choice(prices), rng.choice(sizes)
        notional = str(round(float(price) * float(size), 6))
        ts = int((start + timedelta(seconds=rng.randint(0, 5 * 24 * 3600))).timestamp())
        rows.append((now.isoformat(), ts, f"0xtx{i}", w, c, f"tok{idx}",
                     "Yes" if idx == 0 else "No", idx, side, price, size, notional,
                     f"evt{c}", f"slug{c}-x", "Title", "tape", "{}"))
    con.executemany(
        "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, condition_id,"
        " asset, outcome, outcome_index, side, price, size, notional_usd,"
        " event_slug, slug, title, source, raw_json) VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return wallets


def test_score_state_printrow_matches_the_dict_reference_on_a_seeded_tape(cfg, tmp_path):
    """Equivalence: swapping `dict(r)` for `_PrintRow(r)` in score_state's
    own read loop must not move a single number. `_random_score_state_tape`
    gives dust/SELL/resolved+unresolved/hygiene-triggering variety so there
    is something for positions, open_positions, hygiene_excluded, agg, rates
    and a per-wallet lookup to disagree on if the row object ever leaks a
    wrong value."""
    from arb import whale_mat
    from arb.whale_state import score_state
    rng = random.Random(20260905)
    con = _tape(tmp_path)
    wallets = _random_score_state_tape(con, rng, n_prints=3000)
    whale_mat.materialize(con, cfg, NOW)

    got = score_state(cfg, con, NOW)
    want = _reference_score_state(cfg, con, NOW)

    assert got.positions == want.positions
    assert got.open_positions == want.open_positions
    assert got.hygiene_excluded == want.hygiene_excluded
    assert got.agg == want.agg
    assert got.rates == want.rates and got.rate_n == want.rate_n
    for wallet in wallets:
        assert (got.lookup(wallet, "baseball_mlb", "WINNER")
                == want.lookup(wallet, "baseball_mlb", "WINNER"))


# The 400k-row ABSOLUTE timing guard that used to sit here is gone (matpos,
# 2026-09-05). Two reasons, both from the review of 94ef331: at a 15.0 s bound
# it could not catch a regression (the pre-rowperf code passed it too — the
# bound had already been widened twice to stop it flaking on this dev box,
# which runs the real supervisor against the 763k-row production tape while
# the suite runs), and it measured seconds on a host whose background load
# swings wider than anything it was guarding. What it was reaching for —
# "the 60 s scoring pass does not grow with the tape" — is now asserted
# directly and without a clock: tests/test_whale_mat.py
# ::test_score_state_never_reads_the_prints_table (structural: it issues no
# query against `prints` at all) and ::test_score_state_work_does_not_grow_
# with_the_tape (relative: the SQLite VM steps it costs are unchanged when the
# tape carries 4x the prints for the same positions).


def test_signals_by_id_loads_only_the_ids_it_was_asked_for(cfg, tmp_path):
    """I5(b): every `signals` row ever written was json-parsed for the report
    and the bench, which only ever look up the ids the ledger booked."""
    from arb.whale_metrics import signals_by_id
    con = _tape(tmp_path)
    _signal_row(con, 1, [_whale("0xa")])
    _signal_row(con, 2, [_whale("0xb")])

    assert set(signals_by_id(con, [1])) == {1}
    assert set(signals_by_id(con, [])) == set()


def test_signals_by_id_chunks_past_sqlites_variable_limit(cfg, tmp_path):
    """I1 (final review fix pass): an unchunked `IN (...)` over enough ids
    raises `OperationalError: too many SQL variables` once a single statement's
    bound parameters exceed SQLite's per-statement limit (32766 on this build).
    Chunked at the codebase's own CHUNK=400 (arb/dashboard_data.py) — the same
    bound `_marks_by_bet` already uses for `flat_bets`/`markouts` id lookups —
    no single call ever gets near it."""
    from arb.whale_metrics import signals_by_id
    con = _tape(tmp_path)
    n = 33000                                   # over this build's variable limit
    con.executemany(
        "INSERT INTO signals (id, evaluated_at, condition_id, side, variant, "
        "fired, whale_ids_json) VALUES (?,?,?,?,?,?,?)",
        [(i, NOW.isoformat(), CID, "0", "V2", 1, "[]") for i in range(1, n + 1)])
    con.commit()

    out = signals_by_id(con, list(range(1, n + 1)))

    assert set(out) == set(range(1, n + 1))


def test_the_report_parses_no_signal_the_ledger_never_booked(cfg, tmp_path):
    """Discriminating on the parse itself: an unbooked row the report has no
    use for is never read, so even a corrupt one cannot break the daily."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)                         # books V1 and V6, two bets each
    con.execute("INSERT INTO signals (id, evaluated_at, condition_id, side, variant,"
                " fired, whale_ids_json) VALUES (99, ?, ?, '0', 'V2', 1, '{not json')",
                (NOW.isoformat(), CID))
    con.commit()

    frame = report_whale.whale_frame(cfg, store, now=NOW, con=con)

    assert frame["native"]["signals"] == {"native": 2, "transfer": 0}
