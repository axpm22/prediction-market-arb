"""L14 variant V5 "elite" (design §7, registered 2026-09-04): a RELATIVE
qualification bar — the top `whale.fire.elite_pct` of every scored wallet in
the root — instead of the absolute `min_score_lb`, whose meaning drifts with
the base rate.

Pure where the arithmetic is pure (`cutoff`, `point`), and against a real tape
through `score_state` where the registered POPULATION lives: "every wallet with
at least one resolved position in that root, minus the hygiene-excluded
wallets, point-in-time" is a statement about the aggregation the lane already
builds, and a hand-built `agg` could not prove it.
"""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import whale_elite, whale_score, whale_signal, whale_tape
from arb.config import load
from arb.whale_state import score_state

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
SPORT, MTYPE = "baseball_mlb", "WINNER"
BUCKET = f"{SPORT}:{MTYPE}"


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


# ---------------------------------------------------------------- builders

def _score(lb):
    lb = Decimal(str(lb))
    return whale_score.Score(n_bucket=1, n_sport=1, n_root=1, mu=lb,
                             sd=Decimal("0.01"), lb=lb, lb_report=lb, native=True)


def _pop(lbs, root="sports"):
    """(agg, lookup) for a hand-built population: one resolved outcome each,
    exactly the `(wallet, root)` key `whale_score.aggregate` writes."""
    agg = {(w, root): (1, Decimal(str(lb))) for w, lb in lbs.items()}
    return agg, (lambda w, sport, mt: _score(lbs[w]) if w in lbs else None)


class _State:
    """The two fields whale_elite reads off a WhaleState (score_state)."""

    def __init__(self, agg, lookup):
        self.agg, self.lookup = agg, lookup


# TEN wallets: three clear `min_score_lb` 0.0, seven do not.
TEN = {"w0": "-0.06", "w1": "-0.05", "w2": "-0.04", "w3": "-0.03", "w4": "-0.02",
       "w5": "-0.01", "w6": "-0.005", "w7": "0.01", "w8": "0.03", "w9": "0.05"}


# ------------------------------------------------------------------ cutoff

def test_the_cutoff_is_the_top_elite_pct_of_every_scored_wallet_in_the_root(cfg):
    """Registered wording, 2026-09-04: the reference population is EVERY wallet
    with at least one resolved position in that root — not the qualifying
    roster. Ten wallets, of which three clear `min_score_lb`; at `elite_pct` 20
    the elite are the top 2 of TEN (the cutoff is the 2nd-highest lb), never
    the top 2 of three. A wallet in another root is never in the denominator."""
    assert cfg["whale"]["fire"]["elite_pct"] == 20
    agg, lookup = _pop(TEN)
    agg[("elsewhere", "nonsports")] = (1, Decimal("0.99"))

    assert whale_elite.cutoff(cfg, agg, lookup, SPORT, MTYPE) == (Decimal("0.03"), 10)
    # the qualifying roster (3 wallets) would give no cutoff at all at 20%
    only_qualifying, ql = _pop({k: v for k, v in TEN.items() if Decimal(v) >= 0})
    assert whale_elite.cutoff(cfg, only_qualifying, ql, SPORT, MTYPE) == (None, 3)


def test_the_population_is_the_same_in_every_bucket_of_the_root(cfg):
    """A wallet is scored into THIS bucket by the ordinary transfer chain
    whether or not it has traded it, so membership is identical across the
    root's buckets and only the values differ."""
    agg, lookup = _pop(TEN)
    assert whale_elite.population(agg, "sports") == sorted(TEN)
    assert whale_elite.cutoff(cfg, agg, lookup, SPORT, "TOTAL")[1] == 10


def test_v5_gives_one_point_to_an_elite_whale_and_zero_to_the_rest(cfg):
    """`point_of` V5 is a FILTER on top of the same qualification bar every
    other variant uses: a whale is in the list only because it cleared
    `min_score_lb`, and the percentile then decides whether it votes."""
    ctx = {"elite": Decimal("0.03")}
    at = {"wallet": "w8", "lb": Decimal("0.03")}
    assert whale_signal.point_of("V5", at, cfg, ctx=ctx) == Decimal(1)
    assert whale_signal.point_of("V5", {"wallet": "w7", "lb": Decimal("0.029999")},
                                 cfg, ctx=ctx) == Decimal(0)
    # no cutoff at all (a caller with no scoring state) is NO consensus reading,
    # never a free point — the conservative direction for an exit recount.
    assert whale_signal.point_of("V5", at, cfg, ctx=None) == Decimal(0)


# ------------------------------------------------------- degenerate cutoff

def test_v5_degenerates_to_v1_and_warns_when_the_cutoff_falls_below_min_score_lb(
        cfg, tmp_path, caplog):
    """Measured 2026-09-04: at `elite_pct` 40 the cutoff (-0.0046) sits BELOW
    `min_score_lb`, so every qualifying whale is automatically elite and V5 is
    a copy of V1 (56/56 fires retained — the tell). The lane says so once per
    day rather than reporting a distinct variant that is a copy."""
    con = whale_tape.open_tape(tmp_path / "tape.sqlite")
    low = {f"w{i}": str(Decimal("-0.10") + Decimal("0.01") * i) for i in range(10)}
    index = whale_elite.index(cfg, _State(*_pop(low)))
    cut, n = index.cell(SPORT, MTYPE)
    assert (cut, n) == (Decimal("-0.02"), 10) and cut <= cfg["whale"]["min_score_lb"]
    # a qualifying whale (lb >= the 0.0 bar) is then ALWAYS elite: V5 == V1
    assert whale_signal.point_of("V5", {"lb": Decimal("0.0")}, cfg,
                                 ctx={"elite": cut}) == Decimal(1)

    with caplog.at_level("WARNING", logger="arb.whale"):
        whale_elite.persist(con, index, cfg, NOW)
        whale_elite.persist(con, index, cfg, NOW + timedelta(minutes=1))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"
                and "V5" in r.getMessage()]
    assert len(warnings) == 1                      # once per day, not per cycle
    assert BUCKET in warnings[0].getMessage()
    with caplog.at_level("WARNING", logger="arb.whale"):
        whale_elite.persist(con, index, cfg, NOW + timedelta(days=1))
    assert len([r for r in caplog.records if r.levelname == "WARNING"
                and "V5" in r.getMessage()]) == 2


def test_every_cycles_cutoff_and_population_size_are_persisted(cfg, tmp_path):
    """§3's stated instability (the elite set moves week to week) is only
    auditable if the bar itself is on the record every cycle."""
    con = whale_tape.open_tape(tmp_path / "tape.sqlite")
    for minute in (0, 1):
        index = whale_elite.index(cfg, _State(*_pop(TEN)))
        index.cell(SPORT, MTYPE)
        whale_elite.persist(con, index, cfg, NOW + timedelta(minutes=minute))

    rows = [dict(r) for r in con.execute("SELECT * FROM elite_cutoffs ORDER BY as_of")]
    assert len(rows) == 2                          # one row per bucket per cycle
    assert (rows[0]["root"], rows[0]["bucket"]) == ("sports", BUCKET)
    assert (rows[0]["cutoff"], rows[0]["n"]) == ("0.03", 10)
    assert whale_elite.latest(con) == [rows[-1]]   # the newest per bucket


# ------------------------------------------- the population, off a real tape

def _history(con, wallet, n, edge, resolved_at, wash=False):
    """`n` resolved pregame WINNER positions: bought at `1 - edge` and paid
    out, so the wallet's realized edge is exactly `edge` at every level.
    `wash` adds a same-price SELL one second later — §13's wash flag, which
    excludes the wallet's positions before they are ever scored."""
    price = Decimal(1) - Decimal(str(edge))
    for i in range(n):
        cid = f"0x{wallet}{i}"
        start = resolved_at - timedelta(days=2)
        ts = int(start.timestamp()) - 3600
        con.execute("INSERT INTO markets (condition_id, sport, market_type,"
                    " start_utc, tokens_json, first_seen, last_seen)"
                    " VALUES (?,?,?,?,'[]',?,?)",
                    (cid, SPORT, MTYPE, start.isoformat(), start.isoformat(),
                     start.isoformat()))
        rows = [("BUY", ts, str(price), "1000")]
        if wash:
            rows.append(("SELL", ts + 1, str(price), "1"))
        for j, (side, at, px, size) in enumerate(rows):
            con.execute(
                "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet,"
                " condition_id, asset, outcome_index, side, price, size,"
                " notional_usd, source, raw_json) VALUES (?,?,?,?,?,?,0,?,?,?,?,'tape','{}')",
                (start.isoformat(), at, f"0x{wallet}{i}{j}", wallet, cid, "tok",
                 side, px, size, str(price * Decimal(size))))
        con.execute(
            "INSERT INTO resolutions (condition_id, resolved_at,"
            " winner_outcome_index, status, source, raw_json, fetched_at)"
            " VALUES (?,?,0,'resolved','gamma','{}',?)",
            (cid, resolved_at.isoformat(), resolved_at.isoformat()))
    con.commit()


def test_elite_excludes_hygiene_flagged_wallets_from_the_population(cfg, tmp_path):
    """Registered: hygiene-excluded wallets are OUT of the reference population
    — they are not traders for this purpose. `score_state` aggregates
    `open_positions`, so this costs no extra code; the test pins that it is
    actually true, with the flagged wallet holding the BEST record on the tape
    so its exclusion moves the bar."""
    con = whale_tape.open_tape(tmp_path / "tape.sqlite")
    yesterday = NOW - timedelta(days=1)
    for i in range(10):
        _history(con, f"w{i}", 2, Decimal("0.01") * i, yesterday)
    _history(con, "washer", 2, Decimal("0.30"), yesterday, wash=True)

    state = score_state(cfg, con, NOW)

    assert "washer" not in whale_elite.population(state.agg, "sports")
    cut, n = whale_elite.cutoff(cfg, state.agg, state.lookup, SPORT, MTYPE)
    assert n == 10
    clean = sorted((state.lookup(f"w{i}", SPORT, MTYPE).lb for i in range(10)),
                   reverse=True)
    assert cut == clean[1]
    # the counterfactual, so the exclusion is shown to MOVE the bar and not
    # merely to be stated: the same aggregation over every position, hygiene
    # included, is population B — 11 wallets, the washer's best-on-tape record
    # among them.
    including = whale_score.aggregate(whale_score.resolved_outcomes(
        state.positions, state.resolutions, NOW))
    assert "washer" in whale_elite.population(including, "sports")
    assert whale_elite.cutoff(cfg, including, state.lookup, SPORT, MTYPE)[1] == 11


def test_an_earlier_cycles_cutoff_is_not_moved_by_a_wallet_scored_later(cfg, tmp_path):
    """Point-in-time, the same `resolved_at < as_of` rule the scores obey: a
    position that resolves after a cycle is not in that cycle's population, and
    re-running the cycle reproduces its cutoff exactly."""
    con = whale_tape.open_tape(tmp_path / "tape.sqlite")
    for i in range(10):
        _history(con, f"w{i}", 2, Decimal("0.01") * i, NOW - timedelta(days=1))
    _history(con, "late", 2, Decimal("0.50"), NOW + timedelta(hours=1))

    def cut(at):
        state = score_state(cfg, con, at)
        return whale_elite.cutoff(cfg, state.agg, state.lookup, SPORT, MTYPE)

    early, later = cut(NOW), cut(NOW + timedelta(hours=2))

    assert early[1] == 10 and later[1] == 11
    assert early == cut(NOW)                      # re-running the cycle reproduces it
    assert later[0] > early[0]
