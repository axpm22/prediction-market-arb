"""Lane P: ladder fit, ticker parsing, R and both arms on a synthetic game (no network)."""
import numpy as np

from arb.corr import passing as P


def test_parse_ticker_and_rules():
    r = P.parse("KXNFLRECYDS-26OCT29CARGB-GBMGOLDEN0-25",
                "If Matthew Golden records 25+ receiving yards in the Carolina vs Green Bay Pro Football game")
    assert r["team"] == "GB" and r["side"] == "home" and r["X"] == 25 and r["role"] == "REC"
    assert r["player"] == "Matthew Golden"
    q = P.parse("KXNFLPASSYDS-26OCT26DALPHI-DALDPRESCOTT4-250",
                "If Dak Prescott records 250+ passing yards in the Dallas vs Philadelphia game")
    assert q["team"] == "DAL" and q["side"] == "away" and q["role"] == "QB"
    assert P.parse("KXNFLPASSYDS-26OCT26DALPHI-DALDPRESCOTT4-250", "If Dak records 225+ passing yards in the") is None
    assert P.split_teams("KXNFLRECYDS-26OCT26LACLV") == ("LAC", "LV")


def test_fit_recovers_normal():
    X = np.array([150, 200, 225, 250, 275, 300, 350])
    f = P.fit_normal(X, P.sf(X, 241.0, 55.0))
    assert abs(f[0] - 241) < 0.5 and abs(f[1] - 55) < 0.5
    assert P.fit_normal([200, 250], [0.99, 0.5]) is None  # only one rung in band


def _ladder(role, mu, sd, Xs, team="GB"):
    return [{"ticker": f"{role}{mu}-{x}", "X": x, "role": role, "team": team, "player": role,
             "q": {"bid": p - 0.01, "ask": p + 0.01, "mid": p}}
            for x in Xs for p in [float(P.sf(x, mu, sd))]]


def test_R_and_both_arms(monkeypatch):
    qb = _ladder("QB", 250, 60, [175, 225, 250, 275, 325])
    recs = {"a": _ladder("RA", 80, 30, [40, 60, 80, 100]), "b": _ladder("RB", 60, 25, [30, 50, 70, 90])}
    fq = P.fit_normal([x["X"] for x in qb], [x["q"]["mid"] for x in qb])
    fr = {k: P.fit_normal([x["X"] for x in v], [x["q"]["mid"] for x in v]) for k, v in recs.items()}
    u = {"qb": qb, "recs": recs, "fq": fq, "fr": fr, "R": (fr["a"][0] + fr["b"][0]) / fq[0]}
    assert abs(u["R"] - 140 / 250) < 0.01
    monkeypatch.setattr(P, "NREF", 50)
    P.refits(u, np.random.default_rng(0))
    # R_bar equal to this game's R -> both arms reproduce the market
    f = P.arm_fairs(u, np.full(P.NREF, 140 / 250))
    for arm, lad in (("QB", qb), ("REC", recs["a"] + recs["b"])):
        for x in lad:
            fair, lo, hi = f[arm][x["ticker"]]
            assert abs(fair - x["q"]["mid"]) < 0.02 and lo <= fair <= hi
    # R_bar = 0.5 -> QB arm says QB mean 280 (rungs richer); REC arm scales receivers by 250*.5/140 < 1
    f = P.arm_fairs(u, np.full(P.NREF, 0.5))
    x = qb[2]
    assert abs(f["QB"][x["ticker"]][0] - P.sf(250, 280, 60)) < 0.02
    y = recs["a"][2]
    assert abs(f["REC"][y["ticker"]][0] - P.sf(80, 80 * 125 / 140, 30)) < 0.02
