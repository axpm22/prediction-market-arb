"""arb.mlb.data: outcome mapping, shrinkage, no-lookahead (synthetic, no network)."""
import numpy as np
import pandas as pd

from arb.mlb.data import OUTCOMES, _asof, _cum, map_outcome, shrink_alpha

W = np.array([60, 120, 300, 600, 600, 170], float)
LEAGUE = np.array([.22, .09, .14, .045, .004, .03, .471])


def test_outcome_mapping():
    ev = pd.Series(["strikeout", "strikeout_double_play", "walk", "intent_walk", "hit_by_pitch",
                    "catcher_interf", "single", "double", "triple", "home_run", "field_out",
                    "grounded_into_double_play", "sac_fly", "sac_bunt", "fielders_choice",
                    "field_error", "force_out", "truncated_pa", "caught_stealing_2b", None])
    got = map_outcome(ev)
    assert got[17:].isna().all()
    assert got[:17].tolist() == ["K", "K", "BB", "BB", "BB", "BB", "1B", "2B", "3B", "HR",
                                  "OUT", "OUT", "OUT", "OUT", "OUT", "OUT", "OUT"]


def test_zero_pa_is_league():
    a = shrink_alpha(np.zeros(7), LEAGUE, W)
    assert np.allclose(a / a.sum(), LEAGUE / LEAGUE.sum())
    assert np.isclose(a.sum(), W.mean())


def test_shrinkage_math_and_alpha_sum():
    x = np.array([30, 10, 20, 5, 1, 4, 30], float)  # n = 100
    a = shrink_alpha(x, LEAGUE, W)
    assert np.isclose(a.sum(), 100 + W.mean())
    r = (x[0] + W[0] * LEAGUE[0]) / (100 + W[0])  # K before renormalisation
    assert np.isclose(a[0] / a.sum(), r)  # OUT is the residual, so no renormalisation shift
    assert a[0] / a.sum() > LEAGUE[0]  # 30% K hitter shrinks toward, not past, league


def test_no_lookahead():
    pa = pd.DataFrame({"batter": [1, 1, 1, 2], "game_date": ["2026-05-01", "2026-05-02",
                                                             "2026-05-03", "2026-05-02"],
                       "outcome": ["K", "HR", "HR", "BB"]})
    cum = _cum(pa, "batter")
    assert _asof(cum, 1, "2026-05-01").sum() == 0          # nothing before first date
    got = _asof(cum, 1, "2026-05-02")                      # same-day PA excluded
    assert got[OUTCOMES.index("K")] == 1 and got.sum() == 1
    assert _asof(cum, 1, "2026-05-03")[OUTCOMES.index("HR")] == 1
    assert _asof(cum, 1, "2026-06-01").sum() == 3
    assert _asof(cum, 99, "2026-06-01").sum() == 0         # unknown player -> zero counts
    # adding a PA dated D must not change inputs for a game on D
    later = pd.concat([pa, pd.DataFrame({"batter": [1], "game_date": ["2026-05-02"],
                                         "outcome": ["3B"]})])
    assert np.array_equal(_asof(_cum(later, "batter"), 1, "2026-05-02"), got)
