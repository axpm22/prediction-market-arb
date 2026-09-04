"""Lane bench (PROMPT_BENCH). Every test runs against a hand-built fixture DB:
no live HTTP, no Odds API credit, no read of the production ledger (rule 2)."""
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from arb.bench import (LaneResult, rank, render, rule_of_three, verdict_for,
                       wilson)
from arb.bench_kalshi import (_all_in, _ask_at, _freq_minus_cost, _two_sided,
                              l6_tail)
from arb.bench_lanes import l2_pm_pm
from arb.config import load
from arb.kalshi_history import SCHEMA

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")


def _hist(markets):
    """In-memory corpus. `markets` = [(ticker, event, category, result, candles)]."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for tk, ev, cat, res, candles in markets:
        con.execute("INSERT INTO hist_markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tk, ev, "S", cat, "t", res, "0.5", "1000", "0",
                     "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", "now"))
        con.execute("INSERT INTO hist_candles VALUES (?,?,?,?,?)",
                    (tk, 60, len(candles), json.dumps(candles), "now"))
    return con


def _candle(ts, ask_close=None, ask_low=None, trade_low=None, trade_mean=None,
            bid_close="0.95", trade_high=None):
    c = {"end_period_ts": ts, "yes_ask": {}, "yes_bid": {}, "price": {}}
    if ask_close is not None:
        c["yes_ask"]["close_dollars"] = ask_close
    if ask_low is not None:
        c["yes_ask"]["low_dollars"] = ask_low
    if bid_close is not None:
        c["yes_bid"]["close_dollars"] = bid_close
    if trade_low is not None:
        c["price"]["low_dollars"] = trade_low
    if trade_high is not None:
        c["price"]["high_dollars"] = trade_high
    if trade_mean is not None:
        c["price"]["mean_dollars"] = trade_mean
    return c


# --- statistics ------------------------------------------------------------

def test_wilson_brackets_the_point_estimate():
    lo, hi = wilson(50, 100, 1.96)
    assert lo < Decimal("0.5") < hi
    assert Decimal("0.39") < lo and hi < Decimal("0.61")


def test_wilson_stays_inside_the_unit_interval_at_the_tail():
    """The normal approximation goes negative here; the tail cells this bench
    cares about (longshots, 97c) are exactly where that matters."""
    lo, hi = wilson(0, 200, 1.96)
    assert lo == Decimal(0)
    assert Decimal(0) < hi < Decimal("0.05")


def test_rule_of_three():
    assert rule_of_three(100) == Decimal("0.03")
    assert rule_of_three(0) == Decimal(1)


def test_verdict_ladder():
    d = Decimal
    assert verdict_for(CFG, d("0.05"), d("0.02"), d("0.08"), 10) == "SIGNAL"
    assert verdict_for(CFG, d("-0.05"), d("-0.08"), d("-0.02"), 10) == "SIGNAL"
    # CI wholly inside null_band (0.01) => decisively nothing, lane retires
    assert verdict_for(CFG, d("0.001"), d("-0.004"), d("0.006"), 10) == "NULL"
    # CI spans zero AND leaves the band => more data would change the answer
    assert verdict_for(CFG, d("0.01"), d("-0.20"), d("0.22"), 10) == "UNDERPOWERED"
    assert verdict_for(CFG, None, None, None, 0) == "BLOCKED"


def test_edge_identity_is_frequency_minus_cost():
    """E[profit] per contract = P(win) - c. Six wins in ten at an all-in 0.50
    is a +0.10 edge, and the CI is the Wilson interval shifted by the cost."""
    obs = [(Decimal("0.5"), 1, f"e{i}") for i in range(6)]
    obs += [(Decimal("0.5"), 0, f"e{i}") for i in range(6, 10)]
    est, lo, hi, n, k, nc = _freq_minus_cost(CFG, obs)
    assert (n, k, nc) == (10, 6, 10)
    assert est == Decimal("0.100000")
    assert lo < est < hi


def test_clusters_shrink_n_not_inflate_it():
    """Both sides of one event are one coin flip seen twice (rule 11). Ten
    observations from two events must give a WIDER interval than ten from ten."""
    wide = _freq_minus_cost(CFG, [(Decimal("0.5"), i % 2, f"e{i % 2}") for i in range(10)])
    tight = _freq_minus_cost(CFG, [(Decimal("0.5"), i % 2, f"e{i}") for i in range(10)])
    assert (wide[2] - wide[1]) > (tight[2] - tight[1])


# --- price extraction ------------------------------------------------------

def test_ask_at_takes_the_last_price_before_the_cutoff():
    candles = [_candle(100, ask_close="0.40", bid_close="0.38"),
               _candle(200, ask_close="0.55", bid_close="0.52"),
               _candle(300, ask_close="0.90", bid_close="0.88")]
    assert _ask_at(CFG, candles, 250) == Decimal("0.55")


def test_ask_at_rejects_the_empty_book_trap():
    """ask 1.0000 with nothing behind it is the absence of an offer, not a
    price. Accepting it would book a guaranteed loser at every horizon."""
    assert _ask_at(CFG, [_candle(100, ask_close="1.0000")], 200) is None
    assert _ask_at(CFG, [_candle(100, ask_close="0.0000")], 200) is None
    assert _ask_at(CFG, [_candle(100, ask_close="0.60", bid_close="0.55"),
                         _candle(150, ask_close="1.0000")], 200) == Decimal("0.60")


def test_all_in_cost_charges_the_taker_fee():
    ask = Decimal("0.50")
    assert _all_in(CFG, "Sports", ask) > ask


# --- lanes -----------------------------------------------------------------

def test_l6_cohort_entry_is_executable_not_observed():
    """A market whose ask never reached the band is not in the cohort even if
    it settled YES — measurement rule 1 fills at the ask you would cross."""
    hist = _hist([
        ("A", "e1", "Sports", "yes", [_candle(1, ask_low="0.98", trade_high="0.98")]),
        ("B", "e2", "Sports", "yes", [_candle(1, ask_low="0.50", trade_high="0.50")]),
    ])
    r = l6_tail(CFG, None, hist)
    assert r.n == 1


def test_l6_counts_a_tail_event_as_a_loss():
    hist = _hist([
        ("A", "e1", "Sports", "yes", [_candle(1, ask_low="0.98", trade_high="0.98")]),
        ("B", "e2", "Sports", "no", [_candle(1, ask_low="0.98", trade_high="0.98")]),
    ])
    r = l6_tail(CFG, None, hist)
    assert r.n == 2
    assert r.estimate < 0                       # one in two settled to zero
    assert any("tail events" in d for d in r.detail)


def _ledger(rows):
    """rows = [(kind, verdicts)] as the reconciler writes them."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        "CREATE TABLE flat_bets (id INTEGER PRIMARY KEY, kind TEXT);"
        "CREATE TABLE bet_events (id INTEGER PRIMARY KEY, bet_id INTEGER, "
        "  kind TEXT, payload_json TEXT);"
        "CREATE TABLE opportunities (id INTEGER PRIMARY KEY, kind TEXT);")
    ev = 0
    for i, (kind, passes) in enumerate(rows, 1):
        con.execute("INSERT INTO flat_bets VALUES (?,?)", (i, kind))
        # a pair may be graded over several passes, exactly as reconcile writes it
        for verdicts in (passes if isinstance(passes[0], list) else [passes]):
            ev += 1
            con.execute("INSERT INTO bet_events VALUES (?,?,?,?)",
                        (ev, i, "reconciled", json.dumps({"verdicts": verdicts})))
    return con


def test_l2_never_reads_an_unresolvable_leg_as_agreement():
    """UNRESOLVABLE is missing data. Counting it as a graded leg would have
    reported a settlement-agreement audit that never happened."""
    con = _ledger([("XSPREAD", ["WON", None]),
                   ("XSPREAD", ["UNRESOLVABLE", "UNRESOLVABLE"])])
    r = l2_pm_pm(CFG, con, None)
    assert r.n == 0 and r.verdict == "BLOCKED"


def test_l2_flags_a_non_complementary_pair():
    con = _ledger([("XSPREAD", ["WON", "LOST"]), ("XSPREAD", ["WON", "WON"])])
    r = l2_pm_pm(CFG, con, None)
    assert r.n == 2
    assert r.estimate == Decimal("0.500000")


def test_l2_ignores_same_venue_pairs():
    """A same-venue pair cannot disagree ACROSS venues; letting it into the
    numerator would report an intra-venue grading bug as a venue mismatch."""
    con = _ledger([("ARB", ["WON", "WON"])])
    r = l2_pm_pm(CFG, con, None)
    assert r.n == 0


# --- ranking ---------------------------------------------------------------

def _res(lane, verdict, est, half, credits):
    d = Decimal
    return LaneResult(lane=lane, name=lane, primary="p", n=10, verdict=verdict,
                      estimate=d(est), lo=d(est) - d(half), hi=d(est) + d(half),
                      credits=credits)


def test_rank_puts_verdict_first_then_decisiveness_per_credit():
    free = _res("A", "SIGNAL", "0.05", "0.01", 0)
    paid = _res("B", "SIGNAL", "0.05", "0.01", 400)     # same z, costs credits
    weak = _res("C", "UNDERPOWERED", "0.05", "0.30", 0)
    assert [r.lane for r in rank([paid, weak, free])] == ["A", "B", "C"]


def test_render_lists_every_lane_and_survives_a_blocked_one():
    out = render([_res("A", "SIGNAL", "0.05", "0.01", 0),
                  LaneResult(lane="B", name="B", primary="p")], 600)
    assert "A" in out and "B" in out and "BLOCKED" in out
    assert "600 independent units" in out


def test_one_sided_book_is_not_an_executable_price():
    """A fresh Kalshi market quotes 0.97 ask / 0.01 bid on zero volume. Six of
    the first seven >=97c cohort members were exactly this shape before the
    filter existed — they are absent offers, not near-certainties."""
    ghost = _candle(1, ask_close="0.97", bid_close="0.01")
    assert not _two_sided(CFG, ghost, Decimal("0.97"))
    assert _ask_at(CFG, [ghost], 100) is None
    real = _candle(1, ask_close="0.99", bid_close="0.98")
    assert _two_sided(CFG, real, Decimal("0.99"))


def test_l6_cohort_requires_a_trade_at_the_band():
    """An ask sitting in the band that nothing ever traded through is a quote,
    not a cohort member (STRATEGIES.md L6: markets that ever TRADED >= 97c)."""
    hist = _hist([("A", "e1", "Sports", "yes",
                   [_candle(1, ask_low="0.98", bid_close="0.97")])])
    assert l6_tail(CFG, None, hist).n == 0


def test_a_decisive_negative_is_labelled_adverse_not_a_win():
    """L7 measured a clean SIGNAL that makers LOSE 1.4c/contract. Ranking it
    alongside a favourable SIGNAL without saying so would read as a result
    worth pursuing."""
    good = _res("A", "SIGNAL", "0.05", "0.01", 0)
    bad = _res("B", "SIGNAL", "-0.05", "0.01", 0)
    assert good.label == "SIGNAL favourable"
    assert bad.label == "SIGNAL adverse"
    assert [r.lane for r in rank([bad, good])] == ["A", "B"]


def test_l2_unions_verdicts_across_reconcile_passes():
    """reconcile grades one leg per pass (arb/reconcile.py:72). Reading each
    event independently reported 41 fully graded kalshi:polymarket pairs as
    half-graded ones and starved the audit of its whole sample."""
    con = _ledger([("XSPREAD", [["WON", None], [None, "LOST"]])])
    r = l2_pm_pm(CFG, con, None)
    assert r.n == 1
    assert r.estimate == Decimal("0")


def _tape(con, rows):
    """rows = [(ticker, event, category, [(size, yes_price, taker_side), ...])]"""
    con.execute("ATTACH DATABASE ':memory:' AS tape")
    con.execute("CREATE TABLE tape.hist_trades (ticker TEXT, n INTEGER, "
                "trades_json TEXT, pulled_at TEXT)")
    for tk, ev, cat, trades in rows:
        con.execute("INSERT INTO hist_markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tk, ev, "S", cat, "t", "yes", "0.5", "1000", "0",
                     "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", "now"))
        js = [{"count_fp": str(s), "yes_price_dollars": str(p), "taker_side": side,
               "created_time": f"2026-07-01T00:{i:02d}:00Z"}
              for i, (s, p, side) in enumerate(trades)]
        con.execute("INSERT INTO tape.hist_trades VALUES (?,?,?,?)",
                    (tk, len(js), json.dumps(js), "now"))
    return con


def test_l11_follows_the_taker_direction():
    """A big YES buy followed by a rising price is a WIN for the follower; the
    same drift after a big NO buy is a LOSS. Getting this sign backwards would
    invert the lane's entire conclusion."""
    from arb.bench_markout import l11_prints
    up = [(i + 1, "0.50", "yes") for i in range(20)] + [(9999, "0.50", "yes")] + \
         [(1, f"0.{55 + i}", "yes") for i in range(12)]
    con = _tape(_hist([]), [("A", "e1", "Sports", up)])
    r = l11_prints(CFG, None, con)
    assert r.n > 0
    assert r.estimate > 0, "price rose after a YES print: follower should profit"

    down = [(i + 1, "0.50", "no") for i in range(20)] + [(9999, "0.50", "no")] + \
           [(1, f"0.{55 + i}", "no") for i in range(12)]
    con2 = _tape(_hist([]), [("B", "e2", "Sports", down)])
    r2 = l11_prints(CFG, None, con2)
    assert r2.estimate < 0, "price rose after a NO print: follower should lose"


def test_l11_charges_the_taker_fee():
    """Zero drift must still lose money — the follower crosses and pays."""
    from arb.bench_markout import l11_prints
    flat = [(i + 1, "0.50", "yes") for i in range(20)] + [(9999, "0.50", "yes")] + \
           [(1, "0.50", "yes")] * 12
    con = _tape(_hist([]), [("A", "e1", "Sports", flat)])
    r = l11_prints(CFG, None, con)
    assert r.estimate < 0


def test_l11_ignores_a_degenerate_size_distribution():
    """If every print is the same size the p90 sits at the minimum, and without
    a guard the lane would call all of them 'large' and measure plain tape
    drift rather than a size effect."""
    from arb.bench_markout import l11_prints
    same = [(100, "0.50", "yes")] * 30
    con = _tape(_hist([]), [("A", "e1", "Sports", same)])
    assert l11_prints(CFG, None, con).n == 0


def test_null_is_tested_before_signal():
    """An effect can exclude zero and still be too small to act on. L11 measured
    -0.0038/dollar with CI [-0.0073, -0.0002]: real, and entirely inside the 1c
    band. Calling that SIGNAL would rank it beside a live edge."""
    d = Decimal
    assert verdict_for(CFG, d("-0.0038"), d("-0.0073"), d("-0.0002"), 100) == "NULL"
    # outside the band and excluding zero is still a SIGNAL
    assert verdict_for(CFG, d("-0.05"), d("-0.08"), d("-0.02"), 100) == "SIGNAL"


def test_non_money_lanes_opt_out_of_the_economic_floor():
    """L1's primary is a prevalence, not money per dollar. Judging 0.5%
    prevalence against a 1c-per-dollar band would retire the lane using a
    scale that does not apply to it."""
    d = Decimal
    assert verdict_for(CFG, d("0.005"), d("0.0043"), d("0.0057"), 100) == "NULL"
    assert verdict_for(CFG, d("0.005"), d("0.0043"), d("0.0057"), 100,
                       null_band=d(0)) == "SIGNAL"


# --- L14 whale-consensus lane (design 2026-09-03 §12) -----------------------

def _whale_ledger(tmp_path, rows, variant="V2/flat"):
    """A tmp ledger holding `rows` = [(signal_id, event_key, pnl)] booked under
    `variant`, each with its terminal settled event. Returned read-only, the
    way `arb bench` opens the ledger (rule 9 cannot be approached)."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    from arb.replay import open_ro
    from arb.store import Store
    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    store = Store(tmp_path / "bench.sqlite")
    for i, (signal_id, event_key, pnl) in enumerate(rows):
        fp = f"fp-{variant}-{signal_id}-{event_key}-{i}"
        bet_id = store.insert("flat_bets", {
            "created_at": now.isoformat(), "poll_id": 1, "poll_kind": "whale",
            "fingerprint": fp, "position_key": fp, "kind": "WHALE",
            "category": "sports", "sport": "baseball_mlb",
            "venue_pair": "polymarket:self", "event_key": event_key,
            "event_start_utc": (now + timedelta(hours=3)).isoformat(),
            "start_grade": "nominal",
            "legs_json": _json.dumps([{"venue": "polymarket", "market_type": "WINNER",
                                       "outcome": "Rays", "side": "YES",
                                       "cost": "0.50", "fee_cost": "0.01",
                                       "mid0": "0.50",
                                       "venue_market_id": "0xcond:0xyes"}]),
            "stake_usd": "1", "edge_points": "0.05", "trusted": 0, "lane": "review",
            "quality_flag": "none", "leg_skew_secs": 0.0, "status": "OPEN",
            "variant": variant, "signal_id": signal_id})
        store.insert("bet_events", {
            "bet_id": bet_id, "created_at": now.isoformat(), "kind": "settled",
            "payload_json": _json.dumps({"result": "EXITED", "source": "exit:flip",
                                         "pnl": str(pnl)})})
    store.close()
    return open_ro(tmp_path / "bench.sqlite")


def test_l14_is_registered_with_a_credit_line():
    """`bench.run` skips no lane, but a lane without a credit line scores as if
    it were free — L14 is keyless by construction and says so."""
    from arb.bench_lanes import LANES
    assert "L14" in LANES
    assert int(CFG["bench"]["credits_per_month"]["L14"]) == 0


def test_l14_primary_is_v2_flat_roi_clustered_on_event_key(tmp_path):
    """§12: mean settled ROI per $, V2/flat only, clustered by event_key, at
    the ONE-SIDED 10th/90th percentiles."""
    from arb.bench_whale import l14_whale
    from arb.regrade import _bootstrap_mean_ci
    rows = [(i + 1, f"pm:e{i}", "0.20" if i % 2 else "-0.10") for i in range(12)]
    con = _whale_ledger(tmp_path, rows)

    r = l14_whale(CFG, con, None)

    by_event = {ek: [float(p)] for _, ek, p in rows}
    seed = int(CFG["regrade"]["bootstrap_seed"])
    n_boot = int(CFG["regrade"]["bootstrap_n"])
    mean, lo, hi = _bootstrap_mean_ci(by_event, seed, n_boot, percentiles=(0.10, 0.90))
    two_sided = _bootstrap_mean_ci(by_event, seed, n_boot)
    assert r.n == 12 and r.unit == "signal"
    assert abs(float(r.estimate) - mean) < 1e-6
    assert (abs(float(r.lo) - lo) < 1e-6) and (abs(float(r.hi) - hi) < 1e-6)
    assert float(two_sided[1]) < float(r.lo) and float(r.hi) < float(two_sided[2])
    assert any("ONE-SIDED" in d for d in r.detail)   # the table header says 95% CI


def test_l14_counts_only_v2_flat_and_only_distinct_signal_ids(tmp_path):
    from arb.bench_whale import l14_whale
    con = _whale_ledger(tmp_path, [(1, "pm:a", "0.10"), (1, "pm:a", "0.10"),
                                   (2, "pm:b", "0.10")])
    con.close()
    con = _whale_ledger(tmp_path, [(3, "pm:c", "0.99")], variant="V2/conf")
    r = l14_whale(CFG, con, None)
    assert r.n == 2                              # two signals, V2/flat only
    assert abs(float(r.estimate) - 0.10) < 1e-9


def test_l14_is_underpowered_below_the_pre_registered_decision_n(tmp_path):
    """Six all-positive signals exclude zero. The pre-registration says the
    lane cannot be called before kill.decision_n, so the verdict does not."""
    from arb.bench_whale import l14_whale
    con = _whale_ledger(tmp_path, [(i + 1, f"pm:e{i}", "0.20") for i in range(6)])
    r = l14_whale(CFG, con, None)
    assert r.n < int(CFG["whale"]["kill"]["decision_n"])
    assert r.verdict == "UNDERPOWERED"
    assert "decision_n" in r.note


def test_l14_reports_null_and_killed_when_the_upper_bound_is_below_zero(tmp_path):
    """Kill rule 1: at n >= decision_n with the upper 90% bound below zero the
    lane is killed — reported as NULL with the kill named, never as a SIGNAL
    that happens to be adverse."""
    import copy
    from arb.bench_whale import l14_whale
    cfg = copy.deepcopy(CFG)
    cfg["whale"]["kill"]["decision_n"] = 5
    con = _whale_ledger(tmp_path, [(i + 1, f"pm:e{i}", "-0.30") for i in range(8)])
    r = l14_whale(cfg, con, None)
    assert float(r.hi) < 0
    assert r.verdict == "NULL"
    assert "kill" in r.note.lower()


def test_l14_blocks_on_an_empty_ledger(tmp_path):
    from arb.bench_whale import l14_whale
    r = l14_whale(CFG, _whale_ledger(tmp_path, []), None)
    assert r.verdict == "BLOCKED" and r.n == 0 and r.note
