"""metrics.py — the ONE computation site: confirmed/missed honesty, markout sign,
exit-engine vector (§10.8), headline definition."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.metrics import (exit_policy_pnls, pnl_confirmed_cell, pnl_markout)

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


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
