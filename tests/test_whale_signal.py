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

from arb import whale_exit, whale_score, whale_signal, whale_sizing
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


def _ev(cfg, positions, scores, touch=None, now=NOW, market=None, m0=M0,
        ranked=frozenset(), volume=None, elite=None):
    sigs = whale_signal.evaluate(market or MARKET, positions, _lookup(scores),
                                 _touch() if touch is None else touch,
                                 now, cfg, m0, ranked, volume, elite)
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


def test_v4_counts_only_whales_in_the_latest_leaderboard_snapshot(cfg):
    """§7 (amended 2026-09-04): 1 point per qualifying whale that is ranked,
    0 for one that is not. An unranked whale is still a whale everywhere else."""
    pos, scores = _three()
    s = _ev(cfg, pos, scores, ranked=frozenset({"w0", "w2"}))["V4"]
    assert s.points_yes == Decimal(2) and s.points_no == Decimal(0)
    assert {w["wallet"]: w["points"] for w in s.whales} == {
        "w0": Decimal(1), "w1": Decimal(0), "w2": Decimal(1)}


def test_a_ranked_wallet_below_the_bar_is_never_a_v4_point(cfg):
    """Rank is a FILTER on top of the same qualification bar, never a score
    input: a leaderboard wallet our own scores put below `min_score_lb` never
    reaches `evaluate`'s whale list at all, so V4 cannot count it."""
    pos, scores = _three(lbs=("0.05", "0.04"))
    pos.append(_pos("cold", last=T0 - 90))
    scores["cold"] = _score("-0.01")
    s = _ev(cfg, pos, scores, ranked=frozenset({"w0", "w1", "cold"}))["V4"]
    assert s.points_yes == Decimal(2)
    assert "cold" not in {w["wallet"] for w in s.whales}


def test_v5_counts_only_whales_above_the_elite_cutoff(cfg):
    """§7 V5 (registered 2026-09-04): 1 point per qualifying whale whose
    `score_lb` in THIS bucket is in the top `fire.elite_pct` of the reference
    population, 0 for one below it. The cutoff reaches `evaluate` the way V6's
    market context does — one number per (root, bucket) per cycle."""
    pos, scores = _three(lbs=("0.05", "0.04", "0.03"))
    s = _ev(cfg, pos, scores, elite=Decimal("0.04"))["V5"]
    assert s.points_yes == Decimal(2) and s.points_no == Decimal(0)
    assert {w["wallet"]: w["points"] for w in s.whales} == {
        "w0": Decimal(1), "w1": Decimal(1), "w2": Decimal(0)}


def test_v5_fires_at_two_elite_qualifying_whales(cfg):
    """`fire.min_points.V5` is 2, literal — measured, never calibrated."""
    pos, scores = _three(lbs=("0.05", "0.04", "0.03"))
    assert cfg["whale"]["fire"]["min_points"]["V5"] == 2
    s = _ev(cfg, pos, scores, elite=Decimal("0.04"))["V5"]
    assert (s.fired, s.suppressed_reason, s.points_yes) == (True, None, Decimal(2))
    one = _ev(cfg, pos, scores, elite=Decimal("0.05"))["V5"]
    assert (one.fired, one.suppressed_reason) == (False, "min-points")


def test_without_a_cutoff_v5_evaluates_but_never_fires(cfg):
    """No cutoff at all — an empty population, or a caller with no scoring
    state — is NO consensus reading, never a free point for every whale."""
    pos, scores = _three()
    s = _ev(cfg, pos, scores)["V5"]
    assert (s.points_yes, s.fired, s.suppressed_reason) == (Decimal(0), False, "ratio")


def test_v4_fires_at_three_ranked_qualifying_whales(cfg):
    """`fire.min_points.V4` is 3 literally, like V1: no calibration window."""
    pos, scores = _three()
    assert cfg["whale"]["fire"]["min_points"]["V4"] == 3
    s = _ev(cfg, pos, scores, ranked=frozenset({"w0", "w1", "w2"}))["V4"]
    assert (s.fired, s.suppressed_reason, s.points_yes) == (True, None, Decimal(3))
    two = _ev(cfg, pos, scores, ranked=frozenset({"w0", "w1"}))["V4"]
    assert (two.fired, two.suppressed_reason) == (False, "min-points")


def _dom(lb="0.05", notional="50000", last=N, price="0.50", side=0):
    """One whale dict as `qualifying` shapes it, for V6's four factors."""
    return {"wallet": "w0", "side": side, "lb": Decimal(lb), "last_ts": last,
            "open_notional": Decimal(notional), "fresh_avg_price": Decimal(price),
            "avg_price": Decimal(price), "sybil_dropped": False}


def _ctx6(volume="50000", ask="0.50", now_ts=N):
    return {"volume": None if volume is None else Decimal(volume),
            "now_ts": now_ts, "ask": {0: Decimal(ask) if ask else None}}


def test_v6_ranks_50k_in_a_60k_market_above_50k_in_a_5m_market(cfg):
    """§7 V6: the same money is dominance in a small market and noise in a big
    one. q 0.05 x $50,000 = 2,500 point-dollars over each market's own volume."""
    thin = whale_signal.point_of("V6", _dom(), cfg, ctx=_ctx6(volume="60000"))
    deep = whale_signal.point_of("V6", _dom(), cfg, ctx=_ctx6(volume="5000000"))
    assert thin == Decimal("0.041667") and deep == Decimal("0.000500")


def test_v6_recency_halves_a_print_after_half_life_secs(cfg):
    """R_w: a print made `dominance.half_life_secs` ago carries half the weight
    of one made now — recency is what "the money just moved" means."""
    half = int(cfg["whale"]["dominance"]["half_life_secs"])
    assert whale_signal.point_of("V6", _dom(), cfg, ctx=_ctx6()) == Decimal("0.050000")
    aged = whale_signal.point_of("V6", _dom(last=N - half), cfg, ctx=_ctx6())
    assert aged == Decimal("0.025000")


def test_v6_price_factor_is_zero_at_max_drift_points_below_the_ask(cfg):
    """F_w: a whale that bought at the current touch is fresh information; one
    that bought `fire.max_drift_points` (0.03) below it bought a move that has
    already happened, and carries no dominance at all."""
    assert cfg["whale"]["fire"]["max_drift_points"] == Decimal("0.03")
    at = whale_signal.point_of("V6", _dom(), cfg, ctx=_ctx6(ask="0.53"))
    half = whale_signal.point_of("V6", _dom(), cfg, ctx=_ctx6(ask="0.515"))
    under = whale_signal.point_of("V6", _dom(), cfg, ctx=_ctx6(ask="0.49"))
    assert (at, half, under) == (Decimal(0), Decimal("0.025000"), Decimal("0.050000"))


def test_v6_falls_back_to_min_volume_usd_when_volume_is_unknown(cfg):
    """An unknown venue volume must never read as "tiny market => unbounded
    dominance": it reads as `dominance.min_volume_usd`, exactly like a market
    sitting at the floor. Zero reads the same way."""
    floor = whale_signal.point_of("V6", _dom(), cfg, ctx=_ctx6(volume="5000"))
    assert floor == Decimal("0.500000")
    for volume in (None, "0", "1"):
        assert whale_signal.point_of("V6", _dom(), cfg,
                                     ctx=_ctx6(volume=volume)) == floor


def test_v6_does_not_buy_the_side_the_whales_money_is_against(cfg):
    """The measurement V6 exists for (§0.2, 2026-09-04): 12 of the 56 fired V1
    signals had the whales' net dollars on the side the head count did not buy.
    Four $250 whales on YES against one $50,000 whale on NO: V1 fires YES at
    exactly `fire.ratio`, V6 reads the money and takes the other side."""
    pos = [_pos(f"w{i}", size="500", last=T0 - 30 * i) for i in range(4)]
    scores = {f"w{i}": _score("0.05") for i in range(4)}
    pos.append(_pos("big", idx=1, size="100000", last=T0))
    scores["big"] = _score("0.05")
    sigs = _ev(cfg, pos, scores)
    assert (sigs["V1"].side, sigs["V1"].fired) == (0, True)
    assert (sigs["V6"].side, sigs["V6"].fired) == (1, True)
    assert sigs["V6"].points_no > sigs["V6"].points_yes


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


def test_stale_gate_compares_ask_to_fresh_prints_vwap(cfg):
    """Controller pin, spec §7 item 4 (pinned 2026-09-03): `stale` compares the
    ask to the points-weighted VWAP of the contributing whales' FRESH BUY
    prints (`fresh_avg_price`), not their whole-exposure average
    (`avg_price`). w0 accumulated 3,000 @ 0.30 in a window that has aged out
    of `signal_window_secs`, then added 1,000 @ 0.50 in a fresh one: fresh
    VWAP is 0.50 (+0.03 drift cap clears an 0.52 ask); the whole-exposure
    average is 0.35 (+0.03 = 0.38), which would have suppressed it."""
    cfg["whale"]["fire"]["min_points"]["V1"] = Decimal("1")  # isolate w0's own VWAP
    old_ts = T0 - int(cfg["whale"]["signal_window_secs"]) - 100
    pos = [_pos("w0", price="0.30", size="3000", last=old_ts),
           _pos("w0", price="0.50", size="1000", last=T0)]
    s = _ev(cfg, pos, {"w0": _score("0.05")}, touch=_touch(ask="0.52"))["V1"]
    assert (s.fired, s.suppressed_reason) == (True, None)
    w0 = next(w for w in s.whales if not w["sybil_dropped"])
    assert w0["avg_price"] == Decimal("0.350000")        # whole exposure: unchanged
    assert w0["fresh_avg_price"] == Decimal("0.500000")  # fresh window only: gates `stale`


def test_merge_windows_keys_by_wallet_and_side(cfg):
    """A wallet on both sides of the same market is two contributors, not
    one: `merge_windows` keys by `(wallet, outcome_index)`, and `qualifying`
    gates each key's own newest window against the signal window
    independently. w0's side-0 window has aged out; its side-1 window and
    w1's side-1 window are both fresh."""
    old_ts = T0 - int(cfg["whale"]["signal_window_secs"]) - 100
    pos = [_pos("w0", idx=0, price="0.50", size="3000", last=old_ts),
           _pos("w0", idx=1, price="0.50", size="1000", last=T0),
           _pos("w1", idx=1, price="0.50", size="1000", last=T0 - 30)]
    merged = whale_signal.merge_windows(CID, pos, Decimal(str(cfg["whale"]["min_print_usd"])),
                                        NOW, cfg)
    assert sorted(merged.keys()) == [("w0", 0), ("w0", 1), ("w1", 1)]
    scores = {"w0": _score("0.05"), "w1": _score("0.04")}
    whales = whale_signal.qualifying(CID, pos, _lookup(scores), NOW, cfg)
    assert sorted((w["wallet"], w["side"]) for w in whales) == [("w0", 1), ("w1", 1)]
    total, _ = whale_signal.tally("V1", whales, cfg)
    assert (total[whale_signal.YES], total[whale_signal.NO]) == (0, 2)


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


# ------------------------------------------------- holders gate (§7 item 1b)

def _holders(cfg, n, idx=1, lb="0.05"):
    """`n` whales still HOLDING side `idx` whose prints aged out of
    signal_window_secs: invisible to the fresh tally, standing opposition to
    the holders one."""
    old = N - int(cfg["whale"]["signal_window_secs"]) - 100
    pos = [_pos(f"h{i}", idx=idx, last=old - 30 * i) for i in range(n)]
    return pos, {f"h{i}": _score(lb) for i in range(n)}


def test_a_signal_fresh_at_1_00_but_held_2_to_1_against_does_not_fire(cfg):
    """§7 item 1b (L14 amendment 2026-09-04): condition 1 asks whether the
    whales just moved, 1b whether the whales actually IN this market agree.
    Fresh 3-0 our way, holders 3-4 against: 0.4286 < fire.holders_ratio."""
    pos, scores = _three()
    held, held_scores = _holders(cfg, 4)
    s = _ev(cfg, pos + held, {**scores, **held_scores})["V1"]
    assert (s.ratio, s.points_yes, s.points_no) == (Decimal("1.000000"),
                                                    Decimal(3), Decimal(0))
    assert (s.fired, s.suppressed_reason) == (False, "holders")
    assert s.holders_ratio == q6(Decimal(3) / Decimal(7))


def test_a_signal_held_three_to_one_clears_the_holders_gate(cfg):
    """0.75 >= holders_ratio 0.6667 — the band above exit.flip_ratio."""
    pos, scores = _three()
    held, held_scores = _holders(cfg, 1)
    s = _ev(cfg, pos + held, {**scores, **held_scores})["V1"]
    assert (s.fired, s.suppressed_reason) == (True, None)
    assert s.holders_ratio == Decimal("0.750000")


def test_a_market_no_qualifying_whale_holds_still_fires(cfg, monkeypatch):
    """`total == 0` is no reading at all, never a zero share: nothing standing
    to disagree with. Forced here because `holders` is a superset of `fresh` by
    construction — every fresh whale still holds — so `evaluate` alone cannot
    produce the empty recount that a re-scored exit cycle can."""
    assert whale_signal.holders_share("V1", CID, [], _lookup({}), 0, NOW, cfg) \
        == (None, Decimal(0), Decimal(0))
    pos, scores = _three()
    monkeypatch.setattr(whale_signal, "holders_share",
                        lambda *a, **k: (None, Decimal(0), Decimal(0)))
    s = _ev(cfg, pos, scores)["V1"]
    assert (s.fired, s.holders_ratio) == (True, None)


def test_the_v4_holders_recount_counts_only_ranked_whales(cfg):
    """The recount runs under the SIGNAL'S OWN variant, exactly as §9's flip
    does: unranked standing opposition was never V4 consensus, so it blocks V1
    and not V4 on the very same market."""
    pos, scores = _three()
    held, held_scores = _holders(cfg, 4)
    both = {**scores, **held_scores}
    sigs = _ev(cfg, pos + held, both, ranked=frozenset({"w0", "w1", "w2"}))
    assert sigs["V1"].suppressed_reason == "holders"
    assert (sigs["V4"].fired, sigs["V4"].holders_ratio) == (True, Decimal("1.000000"))


def test_the_entry_gate_and_the_flip_exit_share_one_holders_helper(cfg):
    """One helper, two callers (§7 1b entry, §9 flip exit) — they can never
    drift because there is only one implementation."""
    assert whale_exit.holders_share is whale_signal.holders_share
    pos, scores = _three()
    held, held_scores = _holders(cfg, 4)
    both = {**scores, **held_scores}
    entry = _ev(cfg, pos + held, both)["V1"].holders_ratio
    ratio, ours, total = whale_signal.holders_share(
        "V1", CID, pos + held, _lookup(both), 0, NOW, cfg)
    assert (entry, ours, total) == (ratio, Decimal(3), Decimal(7))


# --------------------------------------------- gate order pins (M1, fixer D)

def test_min_points_precedes_holders(cfg):
    """Both gates would fail at once: only 2 fresh whales (V1's min_points is
    3) AND 4 old whales holding the other side (holders would also fail, same
    shape as `_holders`). `min-points` sits earlier in the chain and must
    win — mirrors the sibling `..._precedes_the_later_gates` tests."""
    pos, scores = _three(lbs=("0.05", "0.04"))
    held, held_scores = _holders(cfg, 4)
    s = _ev(cfg, pos + held, {**scores, **held_scores})["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "min-points")


def test_uncalibrated_precedes_holders(cfg):
    """Same dual failure under a null-threshold variant: V3 is `uncalibrated`
    regardless of the holders reading, which would also fail here (3 fresh
    vs. 4 old holders, same as `_holders`)."""
    pos, scores = _three()
    held, held_scores = _holders(cfg, 4)
    sigs = _ev(cfg, pos + held, {**scores, **held_scores})
    assert sigs["V3"].suppressed_reason == "uncalibrated"


def test_holders_precedes_late_print(cfg):
    """Both gates fail at once: the fresh tally is held 3-4 against (holders
    fails) AND the newest fresh print is already >= max_print_age_secs
    (late-print would also fail). `holders` sits earlier in the chain (spec
    §13, amended 2026-09-04) and must win."""
    age = int(cfg["whale"]["max_print_age_secs"])
    pos = [_pos(f"w{i}", last=N - age - 30 * i) for i in range(3)]
    scores = {f"w{i}": _score("0.05") for i in range(3)}
    held, held_scores = _holders(cfg, 4)
    s = _ev(cfg, pos + held, {**scores, **held_scores})["V1"]
    assert (s.fired, s.suppressed_reason) == (False, "holders")


# ------------------------------------------- holders computed once (M3, fixer D)

def test_holders_list_is_computed_once_per_market_not_per_variant(cfg, monkeypatch):
    """`holders_share` used to re-run `qualifying(..., fresh_only=False)`
    itself once per variant (6x/market, one per `_signal` call) — `evaluate`
    now computes the holders list once and hands it to every variant."""
    pos, scores = _three()
    fresh_only_values = []
    real = whale_signal.qualifying

    def counting(*a, **k):
        fresh_only_values.append(k.get("fresh_only", True))
        return real(*a, **k)

    monkeypatch.setattr(whale_signal, "qualifying", counting)
    sigs = _ev(cfg, pos, scores)

    assert len(sigs) == 6                        # every variant still evaluated
    assert fresh_only_values.count(False) == 1    # was 6 (once per variant) before the fix


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
    assert sorted(_ev(cfg, pos, scores)) == ["V1", "V2", "V3", "V4", "V5", "V6"]


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


def test_a_v4_exit_recomputes_the_flip_over_ranked_whales_only(cfg):
    """§9 under the fourth variant: the flip recomputation runs the SAME
    variant the bet was booked under, so unranked opposition is not a V4
    consensus. One book, two answers: V1 flips at 2-of-5, V4 holds at 2-of-3."""
    pos, scores = _held()
    for i in range(3):
        pos.append(_pos(f"a{i}", idx=1, last=T0 - 60 - 30 * i))
        scores[f"a{i}"] = _score("0.05")
    ranked = frozenset({"w0", "w1", "a0"})
    v1 = whale_exit.exit_reason(_ctx("V1"), pos, _lookup(scores), NOW, cfg, ranked)
    assert (v1.reason, v1.detail["ratio"]) == ("flip", Decimal("0.400000"))
    assert whale_exit.exit_reason(_ctx("V4"), pos, _lookup(scores), NOW, cfg,
                                  ranked) is None


def test_a_v5_bet_is_never_flipped_by_non_elite_opposition(cfg):
    """§9 under the fifth variant: the flip recount runs the BET'S OWN variant,
    so whales below the elite cutoff were never a V5 consensus and cannot flip
    a V5 position. One book, two answers: V1 flips at 2-of-5, V5 holds."""
    pos, scores = _held()
    for i in range(3):
        pos.append(_pos(f"a{i}", idx=1, last=T0 - 60 - 30 * i))
        scores[f"a{i}"] = _score("0.01")               # qualifying, not elite
    ctx = {"elite": Decimal("0.05")}
    v1 = whale_exit.exit_reason(_ctx("V1"), pos, _lookup(scores), NOW, cfg, market_ctx=ctx)
    assert (v1.reason, v1.detail["ratio"]) == ("flip", Decimal("0.400000"))
    assert whale_exit.exit_reason(_ctx("V5"), pos, _lookup(scores), NOW, cfg,
                                  market_ctx=ctx) is None


def test_v5_confidence_exit_is_none_when_no_remaining_whale_is_elite(cfg):
    """The V4 fix, extended to V5 (§3 implementation notes): a V5 whale's
    points are 1/0 percentile membership, not a score, so once every remaining
    whale has fallen out of the elite set `wmean`'s zero-weight fallback would
    average their raw `lb` back in unweighted — a `confidence` verdict computed
    over a crowd V5 itself says does not count."""
    pos, scores = _held(scores={"w0": _score("-0.05"), "w1": _score("-0.05")})
    assert whale_exit.exit_reason(_ctx("V5"), pos, _lookup(scores), NOW, cfg,
                                  market_ctx={"elite": Decimal("0.05")}) is None


def test_a_v6_bet_flips_on_dollars_not_on_head_count(cfg):
    """§9 under the sixth variant: three $250 holders on our side are not a
    consensus against one whale holding $50,000 on the other. The same book
    leaves a V1 bet held (3-1 by head count) and flips the V6 bet."""
    pos = [_pos(f"w{i}", size="500", last=T0 - 30 * i) for i in range(3)]
    scores = {f"w{i}": _score("0.05") for i in range(3)}
    pos.append(_pos("big", idx=1, size="100000", last=T0))
    scores["big"] = _score("0.05")
    booked = [{"wallet": f"w{i}", "lb": Decimal("0.05"), "side": 0,
               "open_size": Decimal("500")} for i in range(3)]
    v6 = whale_exit.exit_reason(_ctx("V6", whales=booked), pos, _lookup(scores),
                                NOW, cfg)
    assert (v6.reason, v6.detail["ratio"] < Decimal("0.10")) == ("flip", True)
    assert whale_exit.exit_reason(_ctx("V1", whales=booked), pos,
                                  _lookup(scores), NOW, cfg) is None


def test_exit_left_when_the_signal_whales_sold_half(cfg):
    pos, scores = _held(sold="600")                   # 800 open of 2,000 booked
    d = whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg)
    assert (d.reason, d.detail["open_now"]) == ("left", Decimal("800.000000"))
    assert d.detail["via"] == "prints"                # the tape, not the venue


def test_exit_left_via_positions_when_the_holdings_fell_and_no_sell_printed(cfg):
    """Item C of the L14 amendment (2026-09-04): the second `left` test reads
    what data-api `positions?user=` says each signal whale HOLDS now. The
    prints here are unanimous that nothing was sold — every whale still shows
    its full booked window — and the venue says they hold 40% of it. The
    holdings win: a wallet that leaves quietly is exactly what the prints
    cannot see."""
    pos, scores = _held()                             # 2 x 1,000, nothing sold
    assert whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg) is None
    d = whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg,
                               holdings={"w0": Decimal("400"), "w1": Decimal("400")})
    assert (d.reason, d.detail["via"]) == ("left", "positions")
    assert (d.detail["open_now"], d.detail["open_at_booking"]) \
        == (Decimal("800.000000"), Decimal("2000.000000"))


def test_exit_positions_rule_falls_back_to_prints_for_a_wallet_with_no_snapshot(cfg):
    """A wallet the holdings poll has not reached (or whose snapshot is older
    than the max age) is simply ABSENT from the mapping, and its prints-based
    open size stands. Here w0 has sold down to 100 by the venue's reckoning
    and w1 has no reading at all: 1,100 of 2,000 is not half gone, so nothing
    exits on a mixture of one fresh fact and one stale one."""
    pos, scores = _held()
    assert whale_exit.exit_reason(_ctx(), pos, _lookup(scores), NOW, cfg,
                                  holdings={"w0": Decimal("100")}) is None


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


def test_exit_does_not_flip_when_our_prints_aged_out_but_the_whales_still_hold(cfg):
    """The live case (L14 amendment 2026-09-04): 12 of the day's 14 `flip`
    exits fired with points_side 0 and points_total 1-2 after 55-153 minutes,
    because the recount ran over the FRESH-PRINT tally — our four whales'
    prints had simply aged out of signal_window_secs while one opposing fresh
    print remained. Nobody sold: every one of them still holds exactly what it
    held at booking. Ageing out is not a change of mind, so the flip recount
    must read the whales still HOLDING (4 of 5), not the prints still fresh."""
    old = N - int(cfg["whale"]["signal_window_secs"]) - 100
    pos = [_pos(f"w{i}", last=old - 30 * i) for i in range(4)]
    pos.append(_pos("n0", idx=1, last=T0))          # the one fresh opposing print
    scores = {f"w{i}": _score("0.05") for i in range(4)}
    scores["n0"] = _score("0.05")
    ctx = _ctx(whales=[{"wallet": f"w{i}", "lb": Decimal("0.05"), "side": 0,
                        "open_size": Decimal("1000")} for i in range(4)])
    # exactly the reading that fired the bad exits: 0 points ours, 1 total
    fresh, _ = whale_signal.tally(
        "V1", whale_signal.qualifying(CID, pos, _lookup(scores), NOW, cfg), cfg)
    assert (fresh[0], fresh[1]) == (Decimal("0.000000"), Decimal("1.000000"))
    assert whale_exit.exit_reason(ctx, pos, _lookup(scores), NOW, cfg) is None


def test_exit_still_fires_when_ours_sold_out_and_new_whales_bought_the_other_side(cfg):
    """The mirror of the case above: a REAL change of mind still exits. Two of
    the four booked whales sold to zero and two new whales bought the other
    side, so the holders are 2 v 2 (ratio 0.50, not below flip_ratio) and the
    half that left trips `left` — whichever rule the order yields, the
    position exits."""
    pos = [_pos("w0", last=T0), _pos("w1", last=T0 - 30),
           _pos("w2", sold="1000", last=T0 - 60), _pos("w3", sold="1000", last=T0 - 90),
           _pos("n0", idx=1, last=T0), _pos("n1", idx=1, last=T0 - 30)]
    scores = {w: _score("0.05") for w in ("w0", "w1", "w2", "w3", "n0", "n1")}
    ctx = _ctx(whales=[{"wallet": f"w{i}", "lb": Decimal("0.05"), "side": 0,
                        "open_size": Decimal("1000")} for i in range(4)])
    d = whale_exit.exit_reason(ctx, pos, _lookup(scores), NOW, cfg)
    assert d.reason == "left"
    assert (d.detail["open_now"], d.detail["open_at_booking"]) \
        == (Decimal("2000.000000"), Decimal("4000.000000"))


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


def test_exit_open_notional_matches_booking_bit_for_bit(cfg):
    """Item 2, fix 3: `whale_exit.exit_reason` derives each whale's current
    open_notional as `q6(notional)` straight off `merge_windows` — the same
    formula `qualifying` used to book it — instead of re-deriving it through
    a quantized avg_price multiplied back by size, which can round
    differently. w0's two windows (1,000 @ 0.30, 2,000 @ 0.666667) sum to a
    notional that does not divide evenly by size, so the old avg-then-
    multiply path would have drifted a thousandth of a dollar from booking's
    value; this pins the two paths to agree bit-for-bit when nothing changed."""
    scores = {"w0": _score("0.05"), "w1": _score("0.04")}
    pos = [_pos("w0", price="0.333333", size="1000", last=T0),
           _pos("w0", price="0.666667", size="2000", last=T0 - 400),
           _pos("w1", price="0.50", size="1000", last=T0 - 30)]
    booked = _ev(cfg, pos, scores)["V1"].whales
    w0 = next(w for w in booked if w["wallet"] == "w0" and not w["sybil_dropped"])
    now_merge = whale_signal.merge_windows(CID, pos, Decimal(str(cfg["whale"]["min_print_usd"])),
                                           NOW, cfg)
    assert q6(now_merge[("w0", 0)]["notional"]) == w0["open_notional"]


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


def test_v4_confidence_exit_is_none_when_every_remaining_whale_is_unranked(cfg):
    """Fix 2 (V4 review): every remaining whale carries zero V4 points once
    it has fallen off the leaderboard, so `wmean`'s zero-weight fallback
    would otherwise average their raw `lb` back in unweighted — exactly the
    leak V4's rank filter exists to close (a `confidence` verdict computed
    over a crowd V4 itself says does not count). With no ranked whale left
    to weigh, the exit is None rather than a mean of the unranked group."""
    pos, scores = _held(scores={"w0": _score("-0.05"), "w1": _score("-0.05")})
    assert whale_exit.exit_reason(_ctx("V4"), pos, _lookup(scores), NOW, cfg) is None


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


# -------------------------------------------------------------- kelly stake

def test_kelly_stake_worked_examples(cfg):
    """§8 amendment 2026-09-05, the registered vectors: f = kelly_fraction x
    (S - min_score_lb) / (1 - p), clipped to [min_stake_usd, max_stake_usd]."""
    k = whale_sizing.kelly_stake
    assert k(Decimal("0.05"), Decimal("0.90"), cfg) == Decimal("0.125000")
    assert k(Decimal("0.05"), Decimal("0.30"), cfg) == Decimal("0.100000")
    assert k(Decimal("0.30"), Decimal("0.80"), cfg) == Decimal("0.375000")
    assert k(Decimal("0"), Decimal("0.90"), cfg) == Decimal("0.100000")
    assert k(Decimal("0.50"), Decimal("0.95"), cfg) == Decimal("1.000000")


def test_kelly_stake_floors_on_no_edge_and_on_an_unusable_price(cfg):
    """e <= 0 books the floor (every fired signal books a kelly row, like
    conf); an ask of 1 or more cannot happen and is guarded anyway."""
    k = whale_sizing.kelly_stake
    assert k(Decimal("-0.20"), Decimal("0.50"), cfg) == Decimal("0.100000")
    assert k(Decimal("0.50"), Decimal("1"), cfg) == Decimal("0.100000")
    assert k(Decimal("0.50"), Decimal("1.20"), cfg) == Decimal("0.100000")
    assert k(Decimal("0.50"), None, cfg) == Decimal("0.100000")


def test_stake_for_dispatches_the_three_registered_sizings(cfg):
    S, p = Decimal("0.05"), Decimal("0.90")
    assert whale_sizing.SIZINGS == ("flat", "conf", "kelly")
    assert whale_sizing.stake_for("flat", S, p, cfg) == Decimal(1)
    assert whale_sizing.stake_for("conf", S, p, cfg) == whale_sizing.conf_stake(S, cfg)
    assert whale_sizing.stake_for("kelly", S, p, cfg) == Decimal("0.125000")


# --------------------------------------------------------------------- purity

@pytest.mark.parametrize("module", ["whale_signal", "whale_exit", "whale_dominance",
                                    "whale_sizing"])
def test_module_is_pure(module):
    src = (ROOT / "arb" / f"{module}.py").read_text()
    for banned in ("sqlite3", "requests", "utcnow", "now()", "time.time", "open("):
        assert banned not in src, banned
