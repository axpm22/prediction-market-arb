"""L14 Task 6 — pure signal aggregation, fire rule, exits, confidence stake.

Exits live in `arb/whale_exit.py`: same contract, split off on size alone.

No network, no clock, no ledger: `now` is an argument in every call here.
Scores arrive through a dict-backed `score_lookup`, positions are built from
Task 5's `Position` dataclass, and every threshold is read from `config.yaml`.
"""
import copy
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import whale_exit, whale_score, whale_signal
from arb.config import load
from arb.models import q6

ROOT = Path(__file__).resolve().parent.parent

START = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
NOW = START - timedelta(hours=1)          # 3,600 s to start: clears min_secs_to_start
N = int(NOW.timestamp())
T0 = N - 60                                # a 60 s old print: clears max_print_age_secs
CID = "0xcond"
M0 = Decimal("-0.02")


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


# ---------------------------------------------------------------- builders

def _pos(wallet, idx=0, price="0.50", size="1000", sold="0", last=T0,
         pregame=True, cid=CID):
    price, size = Decimal(price), Decimal(size)
    return whale_score.Position(
        wallet=wallet, condition_id=cid, outcome_index=idx, avg_price=price,
        size=size, notional_usd=q6(price * size), entry_ts=last, last_ts=last,
        pregame=pregame, sport="baseball_mlb", market_type="WINNER",
        root="sports", source="tape", sold_size=Decimal(sold),
        sell_value=Decimal(0))


def _score(lb="0.05", n_bucket=10, n_sport=20, n_root=30, native=True):
    lb = Decimal(lb)
    return whale_score.Score(n_bucket=n_bucket, n_sport=n_sport, n_root=n_root,
                             mu=lb, sd=Decimal("0.01"), lb=lb, lb_report=lb,
                             native=native)


def _lookup(mapping):
    return lambda wallet, sport, market_type: mapping.get(wallet)


def _touch(ask="0.51", bid="0.49", depth="500"):
    t = whale_signal.Touch(ask=Decimal(ask) if ask else None,
                           bid=Decimal(bid) if bid else None,
                           depth_usd=Decimal(depth))
    return {0: t, 1: t}


MARKET = {"condition_id": CID, "start_utc": START.isoformat(),
          "sport": "baseball_mlb", "market_type": "WINNER"}


def _ev(cfg, positions, scores, touch=None, now=NOW, market=None, m0=M0):
    sigs = whale_signal.evaluate(market or MARKET, positions, _lookup(scores),
                                 _touch() if touch is None else touch,
                                 now, cfg, m0)
    return {s.variant: s for s in sigs}


def _three(lbs=("0.05", "0.04", "0.03"), idx=0):
    """Three whales on one side, prints 30 s apart so the sybil rule is inert."""
    pos = [_pos(f"w{i}", idx=idx, last=T0 - 30 * i) for i in range(len(lbs))]
    return pos, {f"w{i}": _score(lb) for i, lb in enumerate(lbs)}


# ------------------------------------------------------------------- points

def test_v1_counts_one_point_per_qualifying_whale(cfg):
    pos, scores = _three()
    pos.append(_pos("cold", last=T0 - 90))          # below min_score_lb
    scores["cold"] = _score("-0.01")
    s = _ev(cfg, pos, scores)["V1"]
    assert s.points_yes == Decimal(3) and s.points_no == Decimal(0)


def test_v2_uses_lb_minus_min_score_lb(cfg):
    cfg["whale"]["min_score_lb"] = Decimal("0.01")   # discriminates against lb itself
    pos, scores = _three(lbs=("0.05", "0.03"))
    s = _ev(cfg, pos, scores)["V2"]
    assert s.points_yes == Decimal("0.060000")       # 0.04 + 0.02
    assert whale_signal.points("V2", s.whales, cfg) == s.points_yes


def test_v3_applies_the_log_size_weight(cfg):
    pos, scores = _three(lbs=("0.05",))              # 1,000 @ 0.50 = $500 open
    s = _ev(cfg, pos, scores)["V3"]
    assert s.points_yes == Decimal("0.038908")       # 0.05 * log10(1 + 500/100)


def test_two_whales_three_seconds_apart_count_once(cfg):
    pos = [_pos("hi", last=T0), _pos("lo", last=T0 - 3)]
    s = _ev(cfg, pos, {"hi": _score("0.05"), "lo": _score("0.03")})["V1"]
    dropped = {w["wallet"]: w["sybil_dropped"] for w in s.whales}
    assert s.points_yes == Decimal(1) and dropped == {"hi": False, "lo": True}
    assert whale_signal.points("V1", s.whales, cfg) == Decimal(1)


def test_sybil_chain_counts_once_whatever_the_scores(cfg):
    """A-B 5 s, B-C 3 s, A-C 8 s: single linkage makes the chain ONE whale, and
    which wallet holds the top score must not change the count."""
    pos = [_pos("A", last=T0), _pos("B", last=T0 - 5), _pos("C", last=T0 - 8)]
    s = _ev(cfg, pos, {"A": _score("0.05"), "B": _score("0.03"),
                       "C": _score("0.04")})["V1"]
    assert s.points_yes == Decimal(1)
    assert [w["wallet"] for w in s.whales if not w["sybil_dropped"]] == ["A"]
    flipped = _ev(cfg, pos, {"A": _score("0.03"), "B": _score("0.05"),
                             "C": _score("0.04")})["V1"]
    assert flipped.points_yes == Decimal(1)
    assert [w["wallet"] for w in flipped.whales if not w["sybil_dropped"]] == ["B"]


def _two_windows():
    """w0 bought twice on the same side, 400 s apart, at different prices."""
    return [_pos("w0", price="0.40", size="1000", last=T0),
            _pos("w0", price="0.60", size="3000", last=T0 - 400),
            _pos("w1", price="0.50", size="1000", last=T0 - 30)]


def test_one_wallet_is_one_whale_per_side(cfg):
    scores = {"w0": _score("0.05"), "w1": _score("0.04")}
    s = _ev(cfg, _two_windows(), scores)["V1"]
    assert s.points_yes == Decimal(2)                 # two wallets, not three windows
    assert sorted(w["wallet"] for w in s.whales) == ["w0", "w1"]
    w0 = next(w for w in s.whales if w["wallet"] == "w0")
    assert w0["open_size"] == Decimal("4000.000000")  # summed
    assert w0["avg_price"] == Decimal("0.550000")     # notional-weighted, not 0.50
    assert w0["open_notional"] == Decimal("2200.000000")
    assert w0["last_ts"] == T0                        # newest window


# ------------------------------------------------------------ fire and its refusals

def test_ratio_below_threshold_suppresses_with_ratio(cfg):
    pos, scores = _three()
    pos.append(_pos("other", idx=1, last=T0 - 90))
    scores["other"] = _score("0.05")
    s = _ev(cfg, pos, scores)["V1"]                   # 3 vs 1 = 0.75 < 0.80
    assert (s.fired, s.suppressed_reason, s.ratio) == (False, "ratio", Decimal("0.750000"))


def test_points_below_min_points_suppresses_with_min_points(cfg):
    pos, scores = _three(lbs=("0.05", "0.04"))        # 2 < min_points.V1 = 3
    s = _ev(cfg, pos, scores)["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "min-points")


def test_ask_above_vwap_drift_suppresses_with_stale(cfg):
    pos, scores = _three()                            # VWAP 0.50, drift cap 0.03
    s = _ev(cfg, pos, scores, touch=_touch(ask="0.55"))["V1"]
    assert (s.fired, s.suppressed_reason, s.vwap) == (False, "stale", Decimal("0.500000"))


def test_too_little_time_to_start_suppresses_with_too_close(cfg):
    late = START - timedelta(seconds=300)             # inside min_secs_to_start
    _, scores = _three()
    fresh = int(late.timestamp()) - 60                # prints still young: not `late-print`
    pos = [_pos(f"w{i}", last=fresh - 30 * i) for i in range(3)]
    s = _ev(cfg, pos, scores, now=late)["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "too-close")


def test_old_print_suppresses_with_late_print(cfg):
    _, scores = _three()                              # inside signal_window,
    pos = [_pos(f"w{i}", last=N - 400 - 30 * i)       #   past max_print_age
           for i in range(3)]
    s = _ev(cfg, pos, scores)["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "late-print")


def test_thin_book_suppresses_with_depth(cfg):
    pos, scores = _three()
    s = _ev(cfg, pos, scores, touch=_touch(depth="10"))["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "depth")


def test_missing_bid_suppresses_with_one_sided(cfg):
    pos, scores = _three()
    s = _ev(cfg, pos, scores, touch=_touch(bid=None))["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "one-sided")


# Reason order: each of these fails TWO adjacent gates at once, so a shuffled
# elif chain flips the answer.

def test_ratio_is_checked_before_min_points(cfg):
    pos = [_pos("y", last=T0), _pos("n", idx=1, last=T0 - 30)]   # 0.50 ratio, 1 point
    s = _ev(cfg, pos, {"y": _score("0.05"), "n": _score("0.05")})["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "ratio")


def test_min_points_and_uncalibrated_precede_the_later_gates(cfg):
    _, scores = _three(lbs=("0.05", "0.04"))
    cfg["whale"]["fire"]["min_points"]["V2"] = Decimal("99")
    fresh = [_pos(f"w{i}", last=T0 - 30 * i) for i in range(2)]
    sigs = _ev(cfg, fresh, scores)
    assert sigs["V1"].suppressed_reason == "min-points"     # set threshold, missed
    assert sigs["V2"].suppressed_reason == "min-points"     # a set threshold is compared
    assert sigs["V3"].suppressed_reason == "uncalibrated"   # a null one is not
    old = [_pos(f"w{i}", last=N - 400 - 30 * i) for i in range(2)]
    sigs = _ev(cfg, old, scores)                            # also past max_print_age
    assert sigs["V1"].suppressed_reason == "min-points"     # not `late-print`
    assert sigs["V3"].suppressed_reason == "uncalibrated"   # not `late-print`


def test_late_print_is_checked_before_too_close(cfg):
    late = START - timedelta(seconds=300)             # inside min_secs_to_start
    _, scores = _three()
    old = int(late.timestamp()) - 400                 # and past max_print_age_secs
    pos = [_pos(f"w{i}", last=old - 30 * i) for i in range(3)]
    s = _ev(cfg, pos, scores, now=late)["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "late-print")


def test_stale_is_checked_before_too_close(cfg):
    late = START - timedelta(seconds=300)
    _, scores = _three()
    fresh = int(late.timestamp()) - 60
    pos = [_pos(f"w{i}", last=fresh - 30 * i) for i in range(3)]
    s = _ev(cfg, pos, scores, touch=_touch(ask="0.55"), now=late)["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "stale")


def test_depth_is_checked_before_one_sided(cfg):
    pos, scores = _three()
    s = _ev(cfg, pos, scores, touch=_touch(bid=None, depth="10"))["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "depth")


def test_late_print_is_checked_before_stale(cfg):
    """Both gates fail at once: the newest print is past `max_print_age_secs`
    AND the ask has drifted past `max_drift_points` above VWAP. `late-print`
    sits earlier in the chain (spec §13) and must win."""
    _, scores = _three()                               # VWAP 0.50
    old = [_pos(f"w{i}", last=N - 400 - 30 * i) for i in range(3)]  # past max_print_age
    s = _ev(cfg, old, scores, touch=_touch(ask="0.55"))["V1"]       # and a stale drift
    assert (s.fired, s.suppressed_reason) == (False, "late-print")


def test_too_close_is_checked_before_depth(cfg):
    """Both gates fail at once: only 300 s to start (< `min_secs_to_start`)
    AND depth below `min_executable_usd`. `too-close` sits earlier in the
    chain and must win."""
    late = START - timedelta(seconds=300)
    _, scores = _three()
    fresh = int(late.timestamp()) - 60                 # prints stay young: not `late-print`
    pos = [_pos(f"w{i}", last=fresh - 30 * i) for i in range(3)]
    s = _ev(cfg, pos, scores, touch=_touch(depth="10"), now=late)["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "too-close")


def test_max_print_age_is_exclusive_at_the_boundary(cfg):
    """Spec §7/§13: a signal fires only if the newest print is YOUNGER than
    max_print_age_secs, so age == max already refuses."""
    age = int(cfg["whale"]["max_print_age_secs"])
    _, scores = _three()
    at = [_pos(f"w{i}", last=N - age - 30 * i) for i in range(3)]
    assert _ev(cfg, at, scores)["V1"].suppressed_reason == "late-print"
    under = [_pos(f"w{i}", last=N - age + 1 - 30 * i) for i in range(3)]
    assert _ev(cfg, under, scores)["V1"].fired is True


def test_clean_signal_fires(cfg):
    pos, scores = _three()
    s = _ev(cfg, pos, scores)["V1"]
    assert (s.fired, s.suppressed_reason, s.side) == (True, None, 0)


def test_null_min_points_evaluates_but_never_fires(cfg):
    pos, scores = _three()
    sigs = _ev(cfg, pos, scores)
    assert cfg["whale"]["fire"]["min_points"]["V2"] is None
    for v in ("V2", "V3"):
        assert (sigs[v].fired, sigs[v].suppressed_reason) == (False, "uncalibrated")
        assert sigs[v].points_yes > 0                 # evaluated, not skipped
    cfg["whale"]["fire"]["min_points"]["V2"] = Decimal("0.01")
    assert _ev(cfg, pos, scores)["V2"].fired is True


def test_evaluate_returns_one_signal_per_variant(cfg):
    pos, scores = _three()
    assert sorted(_ev(cfg, pos, scores)) == ["V1", "V2", "V3"]


def test_no_qualifying_whale_returns_no_signals(cfg):
    pos, scores = _three(lbs=("0.05",))
    stale = [_pos("w0", last=N - int(cfg["whale"]["signal_window_secs"]) - 1)]
    assert whale_signal.evaluate(MARKET, stale, _lookup(scores), _touch(),
                                 NOW, cfg, M0) == []


def test_signal_records_whale_provenance_and_flagged_edge(cfg):
    pos, scores = _three(lbs=("0.05", "0.03"))
    scores["w1"] = _score("0.03", n_bucket=0, native=False)
    s = _ev(cfg, pos, scores)["V1"]                   # V1 weights are equal: S = mean lb
    by_wallet = {w["wallet"]: w for w in s.whales}
    assert (by_wallet["w0"]["n_bucket"], by_wallet["w0"]["native"]) == (10, True)
    assert (by_wallet["w1"]["n_bucket"], by_wallet["w1"]["native"]) == (0, False)
    assert s.S == Decimal("0.040000")
    assert s.flagged_edge == Decimal("0.060000")      # S - m0 = 0.04 - (-0.02)


# -------------------------------------------------------------- calibration

def _rows(lbs_by_wallet, notional="500"):
    """One signal row per (wallet, lb) so a wallet can appear repeatedly with a
    moving score — which is what §7.2's per-wallet reduction is for."""
    return [{"whales": [{"wallet": wallet, "lb": Decimal(lb),
                         "open_notional": Decimal(notional)}]}
            for wallet, lbs in lbs_by_wallet.items() for lb in lbs]


def _cal_sample(n=11):
    """n wallets, 11 of them by default so nearest-rank q10 is the 2nd smallest
    and not `min()`. w0 carries three different scores whose median (0.025)
    differs from its min (0.01), max (0.05) and mean (0.0283) — and each of
    those reductions moves q10 to a different value, so the assertion below
    pins the median specifically."""
    lbs = {"w0": ["0.01", "0.025", "0.05"]}
    for i in range(1, n):
        lbs[f"w{i}"] = [str(Decimal("0.01") * (i + 1))]
    return lbs


def test_calibrate_min_points_reproduces_the_spec_formula(cfg):
    out = whale_signal.calibrate_min_points(_rows(_cal_sample()), cfg)
    # per-wallet medians sorted: 0.02, 0.025, 0.03 ... 0.11; ceil(0.10*11) = 2
    assert out["q10"] == Decimal("0.025000")          # min -> 0.02, max -> 0.03
    assert out["V2"] == Decimal("0.075000")           # 3 * q10
    assert out["V3"] == Decimal("0.058361")           # 3 * q10 * log10(1 + 500/100)
    assert out["n_whales"] == 11
    assert out["median_open_notional"] == Decimal("500.000000")


def test_calibrate_min_points_returns_none_below_min_whales(cfg):
    assert cfg["whale"]["fire"]["calibration_min_whales"] == 10
    assert whale_signal.calibrate_min_points(_rows(_cal_sample(9)), cfg) is None


# --------------------------------------------------------------------- exits

def _ctx(variant="V1", side=0, whales=None):
    return {"variant": variant, "side": side, "condition_id": CID,
            "start_utc": START.isoformat(),
            "whales": whales or [{"wallet": "w0", "lb": Decimal("0.05"),
                                  "open_size": Decimal("1000"), "side": side},
                                 {"wallet": "w1", "lb": Decimal("0.05"),
                                  "open_size": Decimal("1000"), "side": side}]}


def _held(sold="0", scores=None):
    pos = [_pos("w0", sold=sold), _pos("w1", sold=sold, last=T0 - 30)]
    return pos, scores or {"w0": _score("0.05"), "w1": _score("0.05")}


def test_exit_none_when_the_signal_still_holds(cfg):
    pos, scores = _held()
    assert whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg) is None


def test_exit_flip_when_the_ratio_falls_below_flip_ratio(cfg):
    pos, scores = _held()
    for i in range(3):                                # 2 vs 3 = 0.40 < 0.50
        pos.append(_pos(f"a{i}", idx=1, last=T0 - 60 - 30 * i))
        scores[f"a{i}"] = _score("0.05")
    d = whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg)
    assert (d.reason, d.detail["ratio"]) == ("flip", Decimal("0.400000"))


def test_exit_left_when_the_signal_whales_sold_half(cfg):
    pos, scores = _held(sold="600")                   # 800 open of 2,000 booked
    d = whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg)
    assert (d.reason, d.detail["open_now"]) == ("left", Decimal("800.000000"))


def test_exit_confidence_names_the_whale_and_its_levels(cfg):
    pos, scores = _held(scores={"w0": _score("-0.05", n_bucket=11, n_sport=22,
                                             n_root=33, native=False),
                                "w1": _score("0.02")})
    d = whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg)
    assert d.reason == "confidence"                   # mean lb -0.015 < -0.01
    assert d.detail["wallet"] == "w0" and d.detail["drop"] == Decimal("0.100000")
    assert (d.detail["n_bucket"], d.detail["n_sport"], d.detail["n_root"]) == (11, 22, 33)


def test_exit_does_not_flip_when_every_print_aged_out(cfg):
    """Nobody sold, the whales still hold, the prints are simply older than
    signal_window_secs: an empty recomputation is no consensus reading at all,
    not a consensus that flipped."""
    old = N - int(cfg["whale"]["signal_window_secs"]) - 100
    pos = [_pos("w0", last=old), _pos("w1", last=old - 30)]
    scores = {"w0": _score("0.05"), "w1": _score("0.05")}
    assert whale_signal.qualifying(CID, pos, _lookup(scores), NOW, cfg) == []
    assert whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg) is None


def test_exit_left_uses_the_merged_held_size(cfg):
    scores = {"w0": _score("0.05"), "w1": _score("0.04")}
    booked = _ev(cfg, _two_windows(), scores)["V1"].whales
    ctx = dict(_ctx(), whales=booked)                 # 4,000 + 1,000 held at booking
    now_pos = [_pos("w0", price="0.40", size="1000", sold="600", last=T0),
               _pos("w0", price="0.60", size="3000", sold="1500", last=T0 - 400),
               _pos("w1", price="0.50", size="1000", sold="600", last=T0 - 30)]
    d = whale_exit.exit_reason(ctx, now_pos, _lookup(scores), NOW, cfg)
    assert d.reason == "left"                         # 2,300 of 5,000 left
    assert d.detail["open_at_booking"] == Decimal("5000.000000")
    assert d.detail["open_now"] == Decimal("2300.000000")


def test_exit_left_merges_all_of_a_wallets_windows_not_just_fresh_ones(cfg):
    """Spec §7, pinned 2026-09-03: `merge_windows` covers ALL of a wallet's
    open windows on this side, however old — the signal window gates
    QUALIFICATION only, through the newest window's `last_ts`. w0's older
    window (3,000) is already outside `signal_window_secs` by booking time;
    before the fix `qualifying` dropped it from the merge entirely (booked
    `open_size` 1,000) while `open_now` summed every window regardless, so
    selling all 3,000 of the old window read as nothing sold at all. Selling
    3,000 of the wallet's true 4,000 (75%) must trip `left`."""
    old_ts = T0 - int(cfg["whale"]["signal_window_secs"]) - 100
    pos = [_pos("w0", price="0.50", size="3000", last=old_ts),
           _pos("w0", price="0.50", size="1000", last=T0)]
    booked = _ev(cfg, pos, {"w0": _score("0.05")})["V1"].whales
    live = [x for x in booked if not x["sybil_dropped"]]
    assert len(live) == 1 and live[0]["wallet"] == "w0"
    assert live[0]["open_size"] == Decimal("4000.000000")  # merged, not just the fresh 1,000
    now_pos = [_pos("w0", price="0.50", size="3000", sold="3000", last=old_ts),
               _pos("w0", price="0.50", size="1000", last=T0)]
    d = whale_exit.exit_reason(_ctx(whales=booked), now_pos, _lookup({"w0": _score("0.05")}), NOW, cfg)
    assert d.reason == "left"
    assert d.detail["open_at_booking"] == Decimal("4000.000000")
    assert d.detail["open_now"] == Decimal("1000.000000")


def test_exit_confidence_v3_weight_matches_the_merged_booking_notional(cfg):
    """`whale_exit.py` used to keep the FIRST window's `avg_price`
    (`avg.setdefault`) times the wallet's total open size — for w0's two
    windows (1,000 @ 0.40 iterated first, 3,000 @ 0.60 second) that gave
    4,000 * 0.40 = 1,600, not the merged notional of 2,200 (1,000*0.40 +
    3,000*0.60) `qualifying` would have booked. V3 weighs each whale by
    `log10(1 + open_notional / unit_usd)`, so the wrong notional shifts the
    confidence-weighted mean `S`; this pins the value the correct
    (`merge_windows`) weight produces (0.043943 under the old bug)."""
    cfg["whale"]["exit"]["min_score_lb"] = Decimal("1")   # force confidence to evaluate S
    now_pos = [_pos("w0", price="0.40", size="1000", last=T0),       # iterated first: the old
               _pos("w0", price="0.60", size="3000", last=T0 - 400),  #   bug would grab this price
               _pos("w1", price="0.50", size="1000", last=T0 - 30)]
    ctx = _ctx(variant="V3", whales=[
        {"wallet": "w0", "lb": Decimal("0.05"), "side": 0, "open_size": Decimal("4000")},
        {"wallet": "w1", "lb": Decimal("0.02"), "side": 0, "open_size": Decimal("1000")}])
    scores = {"w0": _score("0.05"), "w1": _score("0.02")}
    d = whale_exit.exit_reason(ctx, now_pos, _lookup(scores), NOW, cfg)
    assert d.reason == "confidence"
    assert d.detail["S"] == Decimal("0.044418")


def test_exit_confidence_ignores_sybil_dropped_whales(cfg):
    """A sybil-dropped whale counted zero points at fire; it must not carry
    full weight in the confidence mean afterwards."""
    ctx = _ctx(whales=[{"wallet": "w0", "lb": Decimal("0.05"), "side": 0,
                        "open_size": Decimal("1000"), "sybil_dropped": False},
                       {"wallet": "w1", "lb": Decimal("0.05"), "side": 0,
                        "open_size": Decimal("1000"), "sybil_dropped": True}])
    pos, scores = _held(scores={"w0": _score("-0.05"), "w1": _score("0.05")})
    d = whale_exit.exit_reason(ctx, pos, _lookup(scores), NOW, cfg)
    assert d is not None and d.reason == "confidence"  # -0.05 alone, not 0.00 pooled
    assert (d.detail["wallet"], d.detail["S"]) == ("w0", Decimal("-0.050000"))


def test_exit_none_post_start_whatever_the_signal_does(cfg):
    pos, scores = _held(sold="1000")                  # would be `left` pregame
    after = START + timedelta(seconds=60)
    assert whale_exit.exit_reason(_ctx(), pos, _lookup(scores), after, cfg) is None


# --------------------------------------------------------------- conf stake

def test_conf_stake_spans_min_to_max_and_clips(cfg):
    lo = Decimal(str(cfg["whale"]["min_score_lb"]))
    assert whale_signal.conf_stake(lo, cfg) == Decimal("0.100000")
    assert whale_signal.conf_stake(lo + Decimal("0.10"), cfg) == Decimal("1.000000")
    assert whale_signal.conf_stake(lo + Decimal("0.05"), cfg) == Decimal("0.550000")
    assert whale_signal.conf_stake(lo - Decimal("1"), cfg) == Decimal("0.100000")
    assert whale_signal.conf_stake(lo + Decimal("1"), cfg) == Decimal("1.000000")


# --------------------------------------------------------------------- purity

@pytest.mark.parametrize("module", ["whale_signal", "whale_exit"])
def test_module_is_pure(module):
    src = (ROOT / "arb" / f"{module}.py").read_text()
    for banned in ("sqlite3", "requests", "utcnow", "now()", "time.time", "open("):
        assert banned not in src, banned
