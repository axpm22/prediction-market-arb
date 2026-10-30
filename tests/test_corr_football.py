import numpy as np
import pandas as pd
from scipy.stats import norm

from arb.corr.football import final_scores, fit_normal, p_team_over, parse


def test_parse_real_tickers():
    assert parse("KXNFLSPREAD", "KXNFLSPREAD-26OCT29CARGB-GB21", "Green Bay wins by over 20.5 points") == ("NFL", "SPREAD", "GB", 20.5)
    assert parse("KXNFLTEAMTOTAL", "KXNFLTEAMTOTAL-26OCT29CARGB-GB4", "Green Bay over 3.5 points") == ("NFL", "TEAMTOTAL", "GB", 3.5)
    assert parse("KXNFLTOTAL", "KXNFLTOTAL-26OCT29CARGB-27", "Over 26.5 points") == ("NFL", "TOTAL", None, 26.5)
    assert parse("KXNFLGAME", "KXNFLGAME-26OCT29CARGB-GB", "Green Bay") == ("NFL", "GAME", "GB", None)
    assert parse("KXNCAAFSPREAD", "KXNCAAFSPREAD-26OCT29SCSTMORG-MORG23", "Morgan St. wins by over 22.5 points") == ("NCAAF", "SPREAD", "MORG", 22.5)
    assert parse("KXNCAAFTEAMTOTAL", "KXNCAAFTEAMTOTAL-26OCT29FAUUNT-UNT20", "North Texas over 19.5 points") == ("NCAAF", "TEAMTOTAL", "UNT", 19.5)


def test_ladder_fit_recovers_mu_sigma():
    mu, sd = 6.5, 13.0
    # favorite spread rungs (+1), underdog rungs (-1, "dog wins by > L"), winner rungs (L=0)
    s = np.array([1, 1, 1, 1, -1, -1, 1, -1])
    L = np.array([2.5, 6.5, 10.5, 13.5, 3.5, 7.5, 0.0, 0.0])
    y = norm.cdf((s * mu - L) / sd)
    m, sg = fit_normal(s, L, y, (3.0, 10.0))
    assert abs(m - mu) < 1e-3 and abs(sg - sd) < 1e-3


def test_team_total_formula_vs_monte_carlo():
    rng = np.random.default_rng(0)
    muM, sdM, muS, sdS, rho = 7.0, 13.5, 47.0, 13.0, 0.15
    cov = [[sdM ** 2, rho * sdM * sdS], [rho * sdM * sdS, sdS ** 2]]
    M, S = rng.multivariate_normal([muM, muS], cov, 400_000).T
    for x in (17.5, 23.5, 27.5):
        assert abs(p_team_over(x, "fav", muM, sdM, muS, sdS, rho) - ((S + M) / 2 > x).mean()) < 0.004
        assert abs(p_team_over(x, "dog", muM, sdM, muS, sdS, rho) - ((S - M) / 2 > x).mean()) < 0.004


def test_final_scores_from_ladders():
    # true score GB 24, CAR 17
    rows = [("TEAMTOTAL", "GB", 20.5, "yes"), ("TEAMTOTAL", "GB", 24.5, "no"), ("TEAMTOTAL", "CAR", 16.5, "yes"),
            ("TEAMTOTAL", "CAR", 17.5, "no"), ("SPREAD", "GB", 6.5, "yes"), ("SPREAD", "GB", 7.5, "no"),
            ("TOTAL", None, 40.5, "yes"), ("TOTAL", None, 41.5, "no"), ("GAME", "GB", None, "yes")]
    gm = pd.DataFrame(rows, columns=["kind", "team", "line", "result"]).assign(fair=0)
    teams, feas = final_scores(gm)
    assert teams == ["CAR", "GB"] and feas.tolist() == [[17, 24]]
