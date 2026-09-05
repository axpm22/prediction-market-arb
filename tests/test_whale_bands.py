"""L14 price-band grading (amendment 2026-09-05, operator-directed).

Operator: "Make sure when we grade a whale on 'correct trades' we don't just
take people who are taking easy likely trades."

The scorer never counted a raw win rate — realized edge is already per share
against the fill price — but the BAND still paid a premium the wallet did not
earn: measured on the live tape, [0.2,0.4) paid every buyer +0.0276 and
[0.9,1.0] paid -0.0257, so a cheap-side wallet carried +2.8 points anyone
buying there collected and a favourite-taker was debited 2.5. These pin the
fix: every edge is graded against what its own band paid everyone.

Synthetic outcomes and a tmp_path tape only — no live tape, no clock (`as_of`
is always passed).
"""
import copy
from decimal import Decimal
from pathlib import Path

import pytest

from arb import whale_bands, whale_rank, whale_score, whale_tape
from arb.config import load
from arb.models import q6
from arb.whale_state import score_state
# The position builder and the tape fixture are the rank suite's own, so a band
# test can never disagree with it about what a scorable position looks like.
from tests.test_whale_rank import AS_OF, _bucket, _tape

ROOT = Path(__file__).resolve().parent.parent
KEY = ("sports", "basketball_nba", "basketball_nba:WINNER")


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


def _outcomes(positions, res, as_of=AS_OF):
    return whale_score.resolved_outcomes(positions, res, as_of)


def _reference(cfg, outcomes, as_of=AS_OF):
    """The chain EXACTLY as it stood before this amendment: aggregate the raw
    edges, take m0 off them, score. The bit-for-bit target for `band_adjust:
    false` — written out rather than imported so the switch is proved against
    code the switch cannot reach."""
    agg = whale_score.aggregate(outcomes)
    rates = {r: whale_score.base_rate(outcomes, r, as_of, cfg)[0]
             for r in {o.root for o in outcomes}}
    return agg, {k: whale_score.score_for(agg, k[0], k[2], k[3].split(":", 1)[1],
                                          cfg, rates)
                 for k in agg if len(k) == 4}


def _graded(cfg, outcomes, as_of=AS_OF):
    """The assembly `whale_state.score_state` runs: grade every edge against
    its band FIRST, then aggregate, then take m0 off the graded edges."""
    outcomes = whale_bands.adjust(outcomes, whale_bands.table(outcomes, as_of, cfg), cfg)
    return _reference(cfg, outcomes, as_of)


def _mean(agg, wallet):
    n, total = agg[(wallet, "sports")]
    return q6(total / n)


# ------------------------------------------------------------------- band_of

@pytest.mark.parametrize("price,band", [
    ("0", 0), ("0.199999", 0), ("0.2", 1), ("0.399999", 1), ("0.4", 2),
    ("0.599999", 2), ("0.6", 3), ("0.8", 4), ("0.899999", 4), ("0.9", 5),
    ("0.999999", 5), ("1", 5),
])
def test_band_of_is_lower_inclusive_and_upper_exclusive(cfg, price, band):
    """The registered edges [0.2,0.4,0.6,0.8,0.9] make six bands: a price ON an
    edge belongs to the band ABOVE it, and the last band is closed at 1.0."""
    assert whale_bands.band_of(Decimal(price), cfg) == band
    assert len(cfg["whale"]["score"]["price_bands"]) + 1 == 6


# --------------------------------------------------------------- band rates

def _one_band(res, price="0.50", wins=11, losses=9, fillers=29):
    """`fillers` + 1 wallets, every position at `price`, every wallet at the
    same win rate — so the band's own base rate IS each wallet's mean."""
    pos = _bucket("wT", price, wins, losses, res)
    for i in range(fillers):
        pos += _bucket(f"wF{i}", price, wins, losses, res)
    return pos


def test_a_thin_band_falls_back_to_the_root_base_rate(cfg):
    """Below `band_min_n` there is no honest band estimate, so the band is
    graded at m0 — which leaves such a position exactly where the pre-amendment
    score put it."""
    res = {}
    pos = _one_band(res)                                   # 600 at 0.50, band 2
    pos += _bucket("wThin", "0.95", 2, 8, res)             # 10 at 0.95, band 5
    outs = _outcomes(pos, res)
    m0, _ = whale_score.base_rate(outs, "sports", AS_OF, cfg)
    bands = whale_bands.band_base_rates(outs, "sports", AS_OF, cfg)

    assert bands[5] == (m0, 10)                # thin: the root rate, not -0.75
    assert bands[2] == (q6(Decimal("0.05")), 610 - 10)     # fat: its own mean
    assert bands[2][0] != m0                   # ... and the fallback is not a no-op
    assert bands[0] == (m0, 0)                 # an empty band still reports


# ---------------------------------------------------------------- grading

def test_a_wallet_at_exactly_its_bands_base_rate_scores_zero(cfg):
    """The operator's case, stated as arithmetic: a wallet whose every position
    sits in one band, at exactly what that band paid everyone, has demonstrated
    nothing. Its graded mean is 0 and its lower bound is at or below 0."""
    res = {}
    outs = _outcomes(_one_band(res), res)
    agg, scores = _graded(cfg, outs)

    assert _mean(agg, "wT") == Decimal(0)
    s = scores[("wT",) + KEY]
    assert s.mu == Decimal(0) and s.lb <= Decimal(0)


def _two_bands(res):
    """Two wallets with the SAME raw edge (+0.06) in different bands.

    The cheap band pays +0.05 to everyone, the favourite band -0.02, so raw
    they tie and graded the favourite-taker is 7 points better.
    """
    pos = (_bucket("wCheap", "0.30", 18, 32, res)      # 50 at 0.30: mean +0.06
           + _bucket("wFillA", "0.30", 122, 228, res)  # band 1 -> 140/400 = +0.05
           + _bucket("wFav", "0.70", 38, 12, res)      # 50 at 0.70: mean +0.06
           + _bucket("wFillB", "0.70", 234, 116, res))  # band 3 -> 272/400 = -0.02
    return pos


def test_identical_raw_edges_rank_by_what_their_band_paid(cfg):
    res = {}
    outs = _outcomes(_two_bands(res), res)
    raw_agg, raw = _reference(cfg, outs)
    agg, scores = _graded(cfg, outs)
    cheap, fav = ("wCheap",) + KEY, ("wFav",) + KEY

    # raw: a dead heat, which is the whole complaint
    assert _mean(raw_agg, "wCheap") == _mean(raw_agg, "wFav") == q6(Decimal("0.06"))
    assert raw[cheap].lb == raw[fav].lb
    # graded: the band's own pay is off both, and they separate by 0.07
    assert _mean(agg, "wCheap") == q6(Decimal("0.01"))
    assert _mean(agg, "wFav") == q6(Decimal("0.08"))
    assert scores[fav].lb > scores[cheap].lb


@pytest.mark.parametrize("adjust", [False, True])
def test_band_adjust_false_reproduces_the_ungraded_score_bit_for_bit(cfg, adjust):
    """The switch, both ways: off is the pre-amendment score to the last of six
    decimals; on moves it. Anything else means the amendment cannot be rolled
    back and the frozen-clock comparison has no baseline."""
    cfg["whale"]["score"]["band_adjust"] = adjust
    res = {}
    outs = _outcomes(_two_bands(res), res)
    _, want = _reference(cfg, outs)
    _, got = _graded(cfg, outs)

    assert set(got) == set(want)
    same = all(got[k] == want[k] for k in want)
    assert same is not adjust
    if not adjust:
        assert whale_bands.table(outs, AS_OF, cfg) == {}


# ----------------------------------------------------------- the assembly

def _state(cfg, tmp_path, adjust):
    """`score_state` over the rank suite's own six-position tape."""
    cfg["whale"]["score"]["band_adjust"] = adjust
    tmp_path.mkdir(parents=True, exist_ok=True)
    con = whale_tape.open_tape(_tape(tmp_path, cfg, AS_OF))
    try:
        return score_state(cfg, con, AS_OF)
    finally:
        con.close()


def test_score_state_grades_against_the_band_and_the_switch_reaches_it(cfg,
                                                                       tmp_path):
    """The wiring: one assembly, so V5's cutoffs, V6's excess and the V2/V3
    weights read graded skill without a branch of their own. The fixture's six
    positions are far below `band_min_n`, so the band grades at m0."""
    on = _state(cfg, tmp_path / "on", True)
    off = _state(copy.deepcopy(cfg), tmp_path / "off", False)
    m0 = q6(Decimal(str(cfg["whale"]["score"]["base_rate_default"])))

    assert on.bands["sports"][3] == (m0, 6)      # 0.60 is band 3
    assert off.bands == {}
    key = ("0xwhale", "basketball_nba", "WINNER")
    assert on.lookup(*key).lb > off.lookup(*key).lb    # m0 was negative
    assert on.rates["sports"] == m0                    # n 6 < base_rate_min_n


def test_the_rank_table_reports_the_edge_above_what_the_band_paid(cfg, tmp_path):
    """`band_excess` beside `edge_mean`: same six positions, the band's own pay
    taken off. Diagnostics — the column is read, never fed back."""
    state = _state(cfg, tmp_path / "t", True)
    rows = whale_rank.profiles(state, cfg, AS_OF)
    row = [r for r in rows if r["level"] == "bucket"][0]

    assert row["edge_mean"] == q6(Decimal("1.4") / 6)      # 5 x +0.4, 1 x -0.6
    assert row["band_excess"] == q6(Decimal("1.52") / 6)   # ... each +0.02
    assert "band excess" in "\n".join(whale_rank.traders_md(
        {"top": [row], "best": {}}))
    assert "band_excess" in Path("arb/dashboard.html").read_text()


def test_the_module_does_no_io_and_reads_no_clock():
    """Same discipline as whale_score: `as_of` is handed in, never read, which
    is what keeps a band rate point-in-time and the frozen clock provable."""
    mod = (ROOT / "arb" / "whale_bands.py").read_text()
    for banned in ("sqlite3", "requests", "urllib", "datetime.now", "utcnow",
                   "time.time", "open("):
        assert banned not in mod, banned
