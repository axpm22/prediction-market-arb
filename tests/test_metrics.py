"""metrics.py — the ONE computation site: confirmed/missed honesty, markout sign,
exit-engine vector (§10.8), headline definition."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.metrics import (bet_confirmed, bet_pnl_optimistic, exit_policy_pnls,
                         pnl_confirmed_cell, pnl_markout)
from arb.store import Store

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _confirmed_bet(tmp_path, settled_payload, cost="0.5175"):
    """EV (resolution-settled) bet with a confirmed event (cost_sum) and one
    terminal settled event. Returns (store, bet-dict) for bet_confirmed."""
    store = Store(tmp_path / "m.sqlite")
    legs = json.dumps([{"venue": "kalshi", "cost": cost, "outcome": "mets",
                        "side": "YES"}])
    bet_id = store.insert("flat_bets", {"kind": "EV", "stake_usd": "1",
                                        "legs_json": legs, "status": "OPEN"})
    store.insert("bet_events", {"bet_id": bet_id, "kind": "confirmed",
                                "payload_json": json.dumps({"cost_sum": cost})})
    store.insert("bet_events", {"bet_id": bet_id, "kind": "settled",
                                "payload_json": json.dumps(settled_payload)})
    return store, {"id": bet_id, "kind": "EV", "stake_usd": "1", "legs_json": legs}


# payloads mirror settle_bet (settlement.py:73-77): always result + per_leg,
# fraction added on top for UMA partial settlement.
def test_confirmed_void_returns_flat_pnl_not_nested_tuple(tmp_path):
    # VOID -> opt == 0; must be (True, Decimal(0)), not (True, (True, 0)) (H1)
    store, bet = _confirmed_bet(
        tmp_path, {"result": "VOID", "per_leg": ["VOID"], "winner": "VOID"})
    assert bet_confirmed(store, bet) == (True, Decimal(0))


def test_confirmed_partial_passes_through_partial_pnl(tmp_path):
    # PARTIAL fraction 0.4 -> opt = 1*(0.4/0.5175 - 1) = -0.227053; passthrough
    store, bet = _confirmed_bet(
        tmp_path, {"result": "WON", "per_leg": ["WON"], "winner": "mets",
                   "fraction": "0.4"})
    assert bet_confirmed(store, bet) == (True, Decimal("-0.227053"))


def test_confirmed_won_recomputes_on_confirmed_price(tmp_path):
    # WON -> confirmed-entry basis q6(stake*(1-cost_sum)/cost_sum)
    store, bet = _confirmed_bet(
        tmp_path, {"result": "WON", "per_leg": ["WON"], "winner": "mets"})
    assert bet_confirmed(store, bet) == (True, Decimal("0.932367"))


def test_confirmed_lost_returns_negative_stake(tmp_path):
    # LOST -> full stake lost regardless of entry price
    store, bet = _confirmed_bet(
        tmp_path, {"result": "LOST", "per_leg": ["LOST"], "winner": "royals"})
    assert bet_confirmed(store, bet) == (True, Decimal("-1"))


def test_multileg_negrisk_pnl_is_per_leg_sum(tmp_path):
    # 3-leg NEGRISK NO-basket: exactly one outcome happens, so its NO LOSES and
    # the other two NOs WIN (SPEC §7.1: NO-basket payout N-1 per unit).
    # costs 0.30/0.25/0.35 (sum 0.90), stake $1 -> contracts = 1/0.90.
    # per-leg payoff = c0 WON: k*(1-0.30); c1 LOST: -k*0.25; c2 WON: k*(1-0.35)
    #   = k*(0.70 - 0.25 + 0.65) = k*1.10 = (1/0.90)*1.10 = 1.222222
    # (equivalently contracts*((N-1) - sum_c) = (1/0.90)*(2 - 0.90)).
    store = Store(tmp_path / "n.sqlite")
    legs = json.dumps([{"venue": "polymarket", "cost": "0.30", "side": "NO"},
                       {"venue": "polymarket", "cost": "0.25", "side": "NO"},
                       {"venue": "polymarket", "cost": "0.35", "side": "NO"}])
    bet_id = store.insert("flat_bets", {"kind": "NEGRISK", "stake_usd": "1",
                                        "legs_json": legs, "status": "OPEN"})
    store.insert("bet_events", {"bet_id": bet_id, "kind": "settled",
                                "payload_json": json.dumps(
                                    {"result": ["WON", "LOST", "WON"],
                                     "per_leg": ["WON", "LOST", "WON"],
                                     "winner": "outcome1"})})
    bet = {"id": bet_id, "kind": "NEGRISK", "stake_usd": "1", "legs_json": legs}
    assert bet_pnl_optimistic(store, bet) == Decimal("1.222222")


def test_multileg_pending_when_a_leg_unresolved(tmp_path):
    # a leg still unresolved -> whole bet pending (None), never a partial number
    store = Store(tmp_path / "p.sqlite")
    legs = json.dumps([{"venue": "polymarket", "cost": "0.30", "side": "NO"},
                       {"venue": "polymarket", "cost": "0.35", "side": "NO"}])
    bet_id = store.insert("flat_bets", {"kind": "NEGRISK", "stake_usd": "1",
                                        "legs_json": legs, "status": "OPEN"})
    store.insert("bet_events", {"bet_id": bet_id, "kind": "settled",
                                "payload_json": json.dumps(
                                    {"per_leg": ["WON", "PENDING"]})})
    bet = {"id": bet_id, "kind": "NEGRISK", "stake_usd": "1", "legs_json": legs}
    assert bet_pnl_optimistic(store, bet) is None


def test_exit_engine_vector_10_8():
    # EV bet: ask 0.50, Kalshi fee 0.0175 -> c 0.5175, p_hat 0.55, stake $1
    # later poll bid 0.58: exit fee 0.07*0.58*0.42 = 0.017052 -> exit 0.562948
    # P1 (capture 0.8): 0.045448 >= 0.8*0.0325 = 0.026 -> exits, pnl $0.087822
    bet = {"kind": "EV", "stake_usd": "1", "edge_points": "0.0325",
           "legs_json": json.dumps([{"venue": "kalshi", "cost": "0.5175",
                                     "outcome": "mets", "side": "YES"}])}
    quote_series = [{"bid": Decimal("0.53"), "at": NOW + timedelta(minutes=5)},
                    {"bid": Decimal("0.58"), "at": NOW + timedelta(minutes=10)}]
    pnls = exit_policy_pnls(CFG, bet, quote_series, category="sports")
    assert pnls["take_profit"] == Decimal("0.087822")
    assert pnls["hold"] is None  # resolution pays P0; printed beside, not invented here


def test_markout_is_one_sided_adverse_only():
    # v3.4 sign: pnl_markout = pnl_optimistic + sum(min(0, D_h)) * contracts
    # favorable drift never credited (double-counts momentum)
    bet = {"pnl_optimistic": "0.041667", "stake_usd": "1",
           "legs_json": json.dumps([
               {"cost": "0.48", "mid0": "0.47"}, {"cost": "0.48", "mid0": "0.49"}])}
    marks = [{"leg_index": 0, "horizon_secs": 30, "mark_cost": "0.45", "in_play": 0},
             {"leg_index": 1, "horizon_secs": 30, "mark_cost": "0.52", "in_play": 0}]
    # leg0 drift -0.02 (adverse, counted); leg1 +0.03 (favorable, ignored)
    out = pnl_markout(CFG, bet, marks)
    contracts0 = Decimal("1") * Decimal("0.48") / Decimal("0.96") / Decimal("0.48")
    expected = Decimal("0.041667") + Decimal("-0.02") * contracts0
    assert out == expected.quantize(Decimal("0.000001"))


def test_markout_in_play_marks_excluded():
    bet = {"pnl_optimistic": "0.041667", "stake_usd": "1",
           "legs_json": json.dumps([{"cost": "0.48", "mid0": "0.47"}])}
    marks = [{"leg_index": 0, "horizon_secs": 30, "mark_cost": "0.40", "in_play": 1}]
    assert pnl_markout(CFG, bet, marks) == Decimal("0.041667")  # excluded -> no drift term


def test_missed_confirmation_counts_zero_in_cell():
    # NULL-confirmed counts as MISSED, enters the confirmed cell as $0 (bug I8)
    bets = [{"id": 1, "kind": "EV", "confirmed": None, "pnl_if_confirmed": None},
            {"id": 2, "kind": "EV", "confirmed": True,
             "pnl_if_confirmed": Decimal("0.10")}]
    total, n_missed = pnl_confirmed_cell(bets)
    assert total == Decimal("0.10") and n_missed == 1
