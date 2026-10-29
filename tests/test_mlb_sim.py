"""arb/mlb/sim.py + arb/mlb/tilt.py: calibration, game rules, Dirichlet chunks, tilt."""
import numpy as np
import pytest

from arb.mlb import sim, tilt

L = [.224, .094, .142, .044, .004, .031, .461]   # K BB 1B 2B 3B HR OUT, 2025-like
PARK1 = {"HR": 1, "1B": 1, "2B": 1, "3B": 1}


def side(c=500.0, lineup=None, exp_bf=22):
    a = [x * c for x in L]
    lineup = lineup or [a] * 9
    return {"lineup": [{"id": i, "name": str(i), "alpha": al} for i, al in enumerate(lineup)],
            "sp": {"id": 0, "name": "sp", "alpha": a, "exp_bf": exp_bf},
            "pen": {"alpha": a}}


def game(home=None, away=None, regular=True):
    return {"gamePk": 1, "regular_season": regular, "league": L, "park": PARK1,
            "home": home or side(), "away": away or side()}


def only(k):  # alpha concentrated on one outcome
    a = [1e-3] * 7
    a[k] = 1e5
    return a


@pytest.fixture(scope="module")
def avg():
    return sim.simulate(game(), seed=1)


def test_league_average_calibration(avg):
    per_team = avg.runs.mean()
    assert 4.2 <= per_team <= 4.8
    assert 1.0 <= avg.bat_hr.sum(2).mean() <= 1.3
    assert avg.runs.shape == (20000, 2) and avg.bat_tb.shape == (20000, 2, 9)
    assert (avg.runs[:, 0] != avg.runs[:, 1]).all()        # no ties
    assert (avg.bat_rbi.sum(2) <= avg.runs).all()
    assert (avg.bat_tb >= avg.bat_h).all() and (avg.bat_h >= avg.bat_hr).all()


def test_home_skips_bottom9_when_leading():
    # home: 6 HR batters then 3 K batters -> exactly 6 runs per inning; away: all K
    home = side(lineup=[only(sim.HR)] * 6 + [only(sim.K)] * 3)
    away = side(lineup=[only(sim.K)] * 9)
    r = sim.simulate(game(home, away), n_sims=200, n_chunks=2)
    assert (r.runs[:, 1] == 48).all() and (r.runs[:, 0] == 0).all()   # 8 innings only
    assert (r.innings == 9).all()
    # HR batters score every PA (solo HRs, bases empty), K batters never
    assert (r.bat_rbi[:, 1, :6] == r.bat_hr[:, 1, :6]).all()
    assert (r.bat_hr[:, 1, :6] == 8).all() and (r.bat_h[:, 1, 6:] == 0).all()


def test_ghost_runner_raises_10th_scoring():
    reg = sim.simulate(game(regular=True), seed=2)
    post = sim.simulate(game(regular=False), seed=2)
    # with the automatic runner, extra-inning games end in the 10th more often
    end10 = lambda r: (r.innings[r.innings > 9] == 10).mean()
    assert end10(reg) > end10(post) + 0.1
    assert reg.innings.max() <= sim.MAX_INNINGS


def test_dirichlet_chunks():
    def chunk_sd(c):
        r = sim.simulate(game(side(c), side(c)), n_sims=20000, seed=3)
        return np.std([r.runs[r.chunk == k].mean() for k in range(20)])
    assert chunk_sd(5.0) > 3 * chunk_sd(1e7)


def _poisson_runs(n=20000, seed=0):
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.poisson(4.4, n), rng.poisson(4.6, n)])


def test_tilt_recovers_known_shift():
    R = _poisson_runs()
    true = np.exp(0.15 * R[:, 0] - 0.10 * R[:, 1])
    true /= true.sum()
    inds = [R.sum(1) > 8.5, R[:, 1] > R[:, 0], R[:, 0] > 4.5]
    anchors = [(i, float(true @ i)) for i in inds]
    w, th, ess = tilt.fit_tilt(R, anchors)
    for i, m in anchors:
        assert abs(w @ i - m) < 0.005
    assert np.allclose(th, [0.15, -0.10], atol=0.02) and ess < len(R)
    chunk = np.repeat(np.arange(20), 1000)
    wc, thc, essc = tilt.fit_tilt(R, anchors, chunk)
    assert thc.shape == (20, 2) and essc.shape == (20,)
    for i, m in anchors:
        fair, lo, hi = tilt.interval(i, wc, chunk)
        assert abs(fair - m) < 0.005 and lo <= fair <= hi


def test_tilt_no_anchors_uniform():
    R = _poisson_runs(1000)
    w, th, ess = tilt.fit_tilt(R, [])
    assert np.allclose(w, 1 / 1000) and np.allclose(th, 0) and np.isclose(ess, 1000)
    fair, p5, p95, se = tilt.prob(R[:, 1] > R[:, 0], w, None)
    assert np.isclose(fair, (R[:, 1] > R[:, 0]).mean()) and np.isclose(se, 1 / np.sqrt(1000))
