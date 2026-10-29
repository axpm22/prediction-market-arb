"""Settlement mapping, quote hygiene, fee multiplier and fill proxy for arb/mlb/lane.py."""
import json
import os
import types

import numpy as np
import pandas as pd

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
from arb.mlb import lane  # noqa: E402

R = types.SimpleNamespace(
    runs=np.array([[3, 5], [6, 2], [4, 4]]),  # [away, home]
    sp_k=np.array([[7, 2], [3, 9], [5, 5]]),
    bat_hr=np.zeros((3, 2, 9), int), bat_h=np.zeros((3, 2, 9), int))
R.bat_hr[0, 1, 3] = 1
R.bat_h[:, 0, 0] = [0, 2, 1]


def test_indicators_match_kalshi_rules():
    assert lane.indicator(("win", "home", None, None, None), R).tolist() == [True, False, False]
    assert lane.indicator(("total", None, 7.5, None, None), R).tolist() == [True, True, True]
    assert lane.indicator(("spread", "away", 3.5, None, None), R).tolist() == [False, True, False]
    assert lane.indicator(("team", "home", 4.5, None, None), R).tolist() == [True, False, False]
    assert lane.indicator(("ks", "away", None, 6, None), R).tolist() == [True, False, False]
    assert lane.indicator(("hr", "home", None, 1, 3), R).tolist() == [True, False, False]
    assert lane.indicator(("hit", "away", None, 2, 0), R).tolist() == [False, True, False]


def test_name_match_handles_accents_and_suffixes():
    inp = {"home": {"lineup": [{"name": n} for n in
                               ["Ronald Acuña Jr.", "Ozzie Albies", "Matt Olson"] + ["X Y"] * 6]}}
    assert lane.find_slot(inp, "home", "Ronald Acuna") == 0
    assert lane.find_slot(inp, "home", "M. Olson") == 2  # unique last name fallback
    assert lane.find_slot(inp, "home", "Nobody Here") is None


def _c(ts, bid, ask, hi=None, lo=None, vol=0):
    return {"end_period_ts": ts, "yes_bid": {"close": bid}, "yes_ask": {"close": ask},
            "price": {"high": hi, "low": lo}, "volume": vol}


def test_quote_discards_stale_crossed_and_applies_latency():
    T = 10_000
    stale = lane.candle_rows(json.dumps([_c(T - 700, "0.40", "0.42")]))
    assert lane.quote(stale, T) is None
    crossed = lane.candle_rows(json.dumps([_c(T - 60, "0.42", "0.42")]))
    assert lane.quote(crossed, T) is None
    ok = lane.candle_rows(json.dumps([_c(T - 60, "0.40", "0.42"), _c(T + 60, "0.39", "0.44", "0.44", "0.44", 7)]))
    q = lane.quote(ok, T)
    assert (q["entry_ask"], q["entry_bid"]) == (0.44, 0.39) and len(q["fw"]) == 1


def test_fee_multiplier_switches_on_2026_08_07():
    import datetime as dt
    before = int(dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc).timestamp())
    after = int(dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc).timestamp())
    assert float(lane.fee_mult(before)) == 1.0 and float(lane.fee_mult(after)) == 0.5
    assert lane.fee(0.40, 10, lane.fee_mult(before)) == 0.17


def test_fill_capped_by_traded_volume_and_never_at_mid():
    fw = pd.DataFrame({"ts": [1, 2], "bid": .3, "ask": .32, "hi": [0.31, 0.33], "lo": 0.3, "vol": [50, 4]})
    t = {"q": {"fw": fw}, "result": "yes"}
    f = lane.fill(t, "yes", 0.32, lane.fee_mult(0), 0.4)
    assert f["contracts"] == 4  # only the candle trading at/through 0.32 counts
    assert abs(f["pnl"] - (4 - 4 * 0.32 - f["cost"] + 4 * 0.32)) < 1e-9
    none = {"q": {"fw": fw.assign(hi=0.30)}, "result": "yes"}
    assert lane.fill(none, "yes", 0.32, lane.fee_mult(0), 0.4) is None
