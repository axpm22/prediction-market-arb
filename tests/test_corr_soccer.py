"""Lane S model, indicator mapping and rule parsing (no network, no data files)."""
import os

import numpy as np

os.chdir(os.path.join(os.path.dirname(__file__), ".."))
from arb.corr import soccer as S  # noqa: E402

R90 = " after 90 minutes plus stoppage time (does not include extra time or penalties), then the market resolves to Yes."


def test_matrix_sums_to_one():
    for rho in (0.0, -0.1):
        M = S.score_matrix([0.4, 1.3, 3.5], [0.9, 1.1, 2.0], rho)
        assert np.allclose(M.sum((1, 2)), 1) and (M >= 0).all()


def test_fit_recovers_synthetic_lambdas():
    anc = [("GAME", "home"), ("GAME", "tie"), ("GAME", "away"), ("TOTAL", 1.5), ("TOTAL", 2.5), ("TOTAL", 3.5)]
    I = [S.indicator(a) for a in anc]
    y = S.probs([1.7, 0.8], [1.1, 1.4], I)
    lam = S.fit_lambdas(I, y)
    assert np.allclose(lam, [[1.7, 1.1], [0.8, 1.4]], atol=1e-3)


def test_indicators_on_toy_matrix():
    M = np.zeros((S.N, S.N))
    M[2, 1] = 0.5  # home 2-1
    M[0, 0] = 0.3
    M[1, 3] = 0.2  # away 3-1
    p = lambda sp: float((M * S.indicator(sp)).sum())  # noqa: E731
    assert np.isclose(p(("BTTS",)), 0.7)
    assert np.isclose(p(("SPREAD", "home", 0.5)), 0.5) and np.isclose(p(("SPREAD", "home", 1.5)), 0)
    assert np.isclose(p(("SPREAD", "away", 1.5)), 0.2)
    assert np.isclose(p(("TEAMTOTAL", "away", 0.5)), 0.7) and np.isclose(p(("TEAMTOTAL", "home", 1.5)), 0.5)
    assert np.isclose(p(("SCORE", 1, 3)), 0.2) and np.isclose(p(("SCORE", 3, 1)), 0)
    assert np.isclose(p(("GAME", "tie")), 0.3) and np.isclose(p(("TOTAL", 2.5)), 0.7)


def test_parse_real_rules():
    # verbatim from INVENTORY.md / open.json samples and markets.sqlite
    P = S.parse
    assert P("KXEPLSCORE", "If the result is a 0-0 draw in the Chelsea FC vs Manchester United professional EPL soccer "
             "game originally scheduled for Oct 31, 2026" + R90) == ("SCORE", 0, 0)
    assert P("KXEPLGAME", "If Chelsea wins the Chelsea vs Manchester United professional EPL soccer game originally "
             "scheduled for Oct 31, 2026" + R90) == ("GAME", "home")
    assert P("KXEPLGAME", "If Tie is the result of the Sunderland vs Leeds United professional EPL soccer game "
             "originally scheduled for Oct 25, 2026" + R90) == ("GAME", "tie")
    assert P("KXEPLTOTAL", "If over 0.5 goals are scored in the Chelsea vs Manchester United professional EPL soccer "
             "game originally scheduled for Oct 31, 2026" + R90) == ("TOTAL", 0.5)
    assert P("KXEPLSPREAD", "If Leeds United wins by more than 1.5 goals in the Sunderland vs Leeds United "
             "professional EPL soccer game originally scheduled for Oct 25, 2026" + R90) == ("SPREAD", "away", 1.5)
    assert P("KXEPLBTTS", "If Chelsea and Manchester United both score a goal in the Chelsea vs Manchester United "
             "EPL match originally scheduled for Oct 31, 2026" + R90) == ("BTTS",)
    assert P("KXEPLTEAMTOTAL", "If Chelsea records above 0.5 goals in the Chelsea vs Manchester United professional "
             "EPL soccer game originally scheduled for Oct 31, 2026" + R90) == ("TEAMTOTAL", "home", 0.5)
    # ticker FCA3SCH4: away winner, goals listed winner-first -> (home 3, away 4)
    assert P("KXBUNDESLIGASCORE", "If Schalke 04 wins 4-3 in the FC Augsburg vs Schalke 04 professional Bundesliga "
             "soccer game originally scheduled for Aug 30, 2026" + R90) == ("SCORE", 3, 4)
    assert P("KXBUNDESLIGAGAME", "If Mainz wins the Mainz vs Bremen professional Bundesliga soccer game originally "
             "scheduled for Oct 24, 2026" + R90) == ("GAME", "home")
    # first-half series and non-90-minute rules are out of scope
    assert P("KXEPL1HTOTAL", "If the total goals scored ... in the 1st Half") is None
    assert P("KXEPLGAME", "If Chelsea wins the Chelsea vs Manchester United game, then the market resolves to Yes.") is None


def test_outcome_score_from_settled_markets():
    import pandas as pd
    m = pd.DataFrame({"league": "EPL", "game_key": "g", "result": ["yes", "no", "yes", "no", "yes"],
                      "spec": [("GAME", "home"), ("TOTAL", 2.5), ("TEAMTOTAL", "away", 0.5),
                               ("TEAMTOTAL", "home", 1.5), ("TOTAL", 1.5)]})
    assert S.outcome_scores(m) == {}  # home win with home <= 1 but away >= 1: contradictory -> unresolved
    m.loc[len(m)] = ["EPL", "g", "yes", ("SCORE", 2, 1)]
    m.loc[1, "result"] = "yes"  # total > 2.5 consistent with 2-1
    m.loc[3, "result"] = "yes"
    assert S.outcome_scores(m) == {("EPL", "g"): (2, 1)}


def test_rho_mle_recovers_sign():
    rng = np.random.default_rng(0)
    lh, la, rho = 1.4, 1.1, -0.15
    M = S.score_matrix(lh, la, rho)[0].ravel()
    cells = rng.choice(M.size, 20000, p=M)
    r = S.rho_mle(np.full(cells.size, lh), np.full(cells.size, la), cells // S.N, cells % S.N)
    assert abs(r["rho"] - rho) < 3 * r["se"] + 0.01


def test_confirm_sealed(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "OUT", str(tmp_path))
    try:
        S.candles_db("confirmation")
        raise AssertionError("should refuse")
    except SystemExit:
        pass
