"""L14 Task 5 — pure whale scoring: positions, hygiene, hierarchical lower bound.

No network, no clock, no ledger. Every number here is either built in this
file or read from `config.yaml`; the worked table is spec §6's, pinned to 3
decimals with a +-0.001 tolerance.
"""
import copy
import inspect
import json
import math
import random
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import whale_hygiene, whale_score, whale_tape
from arb.config import load
from arb.models import q6

ROOT = Path(__file__).resolve().parent.parent
ARB = ROOT / "arb"
FIX = Path(__file__).resolve().parent / "fixtures"

AS_OF = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
RESOLVED_AT = (AS_OF - timedelta(days=1)).isoformat()
START = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
T = int(START.timestamp())
TOL = Decimal("0.001")


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


# ---------------------------------------------------------------- builders

def _print(wallet="0xw", cid="0xc", idx=0, side="BUY", ts=T, price="0.50",
           size="1000", source="tape", notional=None):
    price, size = Decimal(price), Decimal(size)
    return {"ts": ts, "proxy_wallet": wallet, "condition_id": cid,
            "outcome_index": idx, "side": side, "price": str(q6(price)),
            "size": str(q6(size)),
            "notional_usd": str(q6(Decimal(notional) if notional is not None
                                   else price * size)),
            "source": source, "asset": f"tok{idx}", "outcome": "Yes",
            "event_slug": "e"}


def _market(sport="basketball_nba", mtype="WINNER", start=START):
    return {"sport": sport, "market_type": mtype, "line": None,
            "start_utc": start.isoformat() if start else None, "event_key": "k"}


def _positions(wallet, sport, mtype, edges, resolutions, tag="",
               resolved_at=RESOLVED_AT, pregame=True):
    """One position per target edge: a WINNER bought at `1 - e`, or a LOSER
    bought at `-e`. Both give `e = payout - avg_price` exactly, with sane
    prices for any non-zero e, so a list of edges pins the bucket mean."""
    out = []
    for i, e in enumerate(edges):
        e = Decimal(str(e))
        win = e > 0
        price = q6(Decimal(1) - e) if win else q6(-e)
        cid = f"0x{wallet}{tag}{sport}{mtype}{i}"
        out.append(whale_score.Position(
            wallet=wallet, condition_id=cid, outcome_index=0, avg_price=price,
            size=Decimal("100"), notional_usd=q6(price * 100), entry_ts=T,
            last_ts=T, pregame=pregame, sport=sport, market_type=mtype,
            root=whale_score.root_of(sport, mtype), source="tape",
            sold_size=Decimal(0), sell_value=Decimal(0)))
        resolutions[cid] = {"status": "resolved",
                            "winner_outcome_index": 0 if win else 1,
                            "resolved_at": resolved_at}
    return out


def _score(cfg, positions, resolutions, bucket_key=None, m0="-0.02"):
    rates = {"sports": Decimal(m0), "nonsports": Decimal(m0)}
    got = whale_score.scores(positions, resolutions, AS_OF, cfg, rates)
    return got if bucket_key is None else got[bucket_key]


def _near(got, want):
    assert abs(got - Decimal(str(want))) <= TOL, f"{got} != {want}"


# ---------------------------------------------------------------- positions

def test_two_buys_within_window_form_one_size_weighted_position(cfg):
    prints = [_print(ts=T, price="0.40", size="1000"),
              _print(ts=T + 100, price="0.60", size="3000")]
    pos = whale_score.positions_from_prints(prints, {"0xc": _market()}, cfg)
    assert len(pos) == 1
    assert pos[0].size == Decimal("4000")
    assert pos[0].avg_price == q6(Decimal("0.55"))   # (0.4*1 + 0.6*3) / 4
    assert pos[0].entry_ts == T and pos[0].last_ts == T + 100


def test_a_gap_beyond_the_window_starts_a_second_position(cfg):
    prints = [_print(ts=T), _print(ts=T + 100), _print(ts=T + 500)]
    pos = whale_score.positions_from_prints(prints, {"0xc": _market()}, cfg)
    assert len(pos) == 2
    assert [p.size for p in pos] == [Decimal("2000"), Decimal("1000")]


def test_a_sell_reduces_fifo_and_never_forms_a_position(cfg):
    prints = [_print(ts=T, price="0.40", size="1000"),
              _print(ts=T + 500, price="0.40", size="1000"),
              _print(ts=T + 600, side="SELL", price="0.60", size="1500")]
    res = {}
    pos = whale_score.positions_from_prints(prints, {"0xc": _market()}, cfg, res)
    assert len(pos) == 2                       # the SELL made no position
    assert pos[0].sold_size == Decimal("1000")  # oldest first, fully consumed
    assert pos[0].sell_value == q6(Decimal("600"))
    assert pos[1].sold_size == Decimal("500")
    # e = (sell_value + remaining * payout) / size - avg_price, spec §6
    # sold out at 0.60 against a 0.40 entry: +0.20 even though the market lost
    assert whale_score.realized_edge(pos[0], Decimal(0)) == q6(Decimal("0.20"))
    # half sold at 0.60, half paid 1.00: (300 + 500) / 1000 - 0.40
    assert whale_score.realized_edge(pos[1], Decimal(1)) == q6(Decimal("0.40"))
    assert res.get("orphan_sell", 0) == 0


def test_a_sell_with_nothing_open_is_ignored_and_counted(cfg):
    res = {}
    pos = whale_score.positions_from_prints(
        [_print(side="SELL")], {"0xc": _market()}, cfg, res)
    assert pos == [] and res["orphan_sell"] == 1


def test_a_print_after_start_marks_the_position_not_pregame(cfg):
    mk = {"0xc": _market()}
    early = whale_score.positions_from_prints([_print(ts=T - 60)], mk, cfg)
    late = whale_score.positions_from_prints([_print(ts=T - 60), _print(ts=T + 60)],
                                             mk, cfg)
    assert early[0].pregame is True
    assert late[0].pregame is False              # last BUY is after start_utc


def test_dust_prints_are_ignored_and_counted(cfg):
    res = {}
    pos = whale_score.positions_from_prints(
        [_print(price="0.50", size="100")], {"0xc": _market()}, cfg, res)
    assert pos == [] and res["dust"] == 1        # $50 < min_print_usd 200


def test_activity_backfill_prints_build_positions_and_carry_source(cfg):
    prints = [_print(ts=T, source="activity"), _print(ts=T + 100, source="activity")]
    pos = whale_score.positions_from_prints(prints, {"0xc": _market()}, cfg)
    assert len(pos) == 1 and pos[0].source == "activity"
    # ...and go through the same hygiene call as live prints
    hyg = whale_hygiene.hygiene_flags(prints, pos, cfg)
    assert hyg.wallets.get("0xw", set()) == set()


def test_an_unmappable_market_never_forms_a_scored_position(cfg):
    res = {}
    pos = whale_score.positions_from_prints(
        [_print()], {"0xc": _market(sport=None)}, cfg, res)
    assert pos == [] and res["unmapped"] == 1


# ---------------------------------------------------------------- scoring

@pytest.mark.parametrize("n,mu,sd,lb80,lb90", [
    (5, -0.004, 0.057, -0.052, -0.077),
    (15, 0.018, 0.050, -0.025, -0.047),
    (40, 0.043, 0.041, 0.009, -0.009),
    (120, 0.066, 0.029, 0.042, 0.029),
    (400, 0.077, 0.019, 0.061, 0.053),
])
def test_spec_6_worked_table(cfg, n, mu, sd, lb80, lb90):
    res = {}
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * n, res)
    s = _score(cfg, pos, res, ("0xw", "sports", "baseball_mlb",
                               "baseball_mlb:WINNER"))
    assert (s.n_bucket, s.n_sport, s.n_root) == (n, n, n)
    _near(s.mu, mu)
    _near(s.sd, sd)
    _near(s.lb, lb80)
    _near(s.lb_report, lb90)


def test_variance_limits_at_zero_and_at_a_million(cfg):
    sd = cfg["whale"]["score"]["prior_sd"]
    ov = float(cfg["whale"]["score"]["obs_var"])
    _, var0 = whale_score.posterior(cfg, Decimal("-0.02"),
                                    [(0, 0.0), (0, 0.0), (0, 0.0)])
    want = float(sd["root"]) ** 2 + float(sd["sport"]) ** 2 + float(sd["bucket"]) ** 2
    assert math.isclose(var0, want, rel_tol=1e-12)
    n = 10 ** 6
    _, var = whale_score.posterior(cfg, Decimal("-0.02"),
                                   [(n, 0.08), (n, 0.08), (n, 0.08)])
    assert abs(var - ov / n) < 1e-9


def test_void_resolutions_are_excluded_from_n(cfg):
    res = {}
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 5, res)
    for cid in list(res)[:2]:
        res[cid] = {"status": "void", "winner_outcome_index": None,
                    "resolved_at": RESOLVED_AT}
    s = _score(cfg, pos, res, ("0xw", "sports", "baseball_mlb",
                               "baseball_mlb:WINNER"))
    assert s.n_bucket == 3


def test_only_pregame_positions_are_scored(cfg):
    res = {}
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 5, res)
    pos += _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 4, res,
                      tag="live", pregame=False)
    s = _score(cfg, pos, res, ("0xw", "sports", "baseball_mlb",
                               "baseball_mlb:WINNER"))
    assert s.n_bucket == 5


def test_positions_resolved_after_as_of_are_invisible(cfg):
    res = {}
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 5, res)
    pos += _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 7, res, tag="future",
                      resolved_at=(AS_OF + timedelta(hours=1)).isoformat())
    s = _score(cfg, pos, res, ("0xw", "sports", "baseball_mlb",
                               "baseball_mlb:WINNER"))
    assert s.n_bucket == 5


def test_transfer_from_mlb_lifts_a_thin_nba_bucket(cfg):
    res = {}
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 200, res)
    pos += _positions("0xw", "basketball_nba", "WINNER", [0.20, -0.30, 0.10], res)
    s = _score(cfg, pos, res, ("0xw", "sports", "basketball_nba",
                               "basketball_nba:WINNER"))
    assert (s.n_bucket, s.n_sport, s.n_root) == (3, 3, 203)
    _near(s.mu, 0.044)
    _near(s.sd, 0.045)
    _near(s.lb, 0.006)
    assert s.native is False          # qualifies only through pooled evidence

    bare = {}
    lone = _positions("0xw", "basketball_nba", "WINNER", [0.20, -0.30, 0.10], bare)
    s2 = _score(cfg, lone, bare, ("0xw", "sports", "basketball_nba",
                                  "basketball_nba:WINNER"))
    _near(s2.lb, -0.067)
    assert s2.native is False


def test_score_for_an_untraded_bucket_uses_sport_and_root_evidence(cfg):
    res = {}
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 400, res)
    outs = whale_score.resolved_outcomes(pos, res, AS_OF)
    agg = whale_score.aggregate(outs)
    s = whale_score.score_for(agg, "0xw", "basketball_nba", "WINNER", cfg,
                              {"sports": Decimal("-0.02")})
    assert (s.n_bucket, s.n_sport, s.n_root) == (0, 0, 400)
    _near(s.mu, 0.060)
    _near(s.sd, 0.042)
    _near(s.lb, 0.024)
    assert s.native is False


def test_a_strong_own_bucket_record_is_native(cfg):
    res = {}
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 400, res)
    s = _score(cfg, pos, res, ("0xw", "sports", "baseball_mlb",
                               "baseball_mlb:WINNER"))
    assert s.native is True and s.lb >= cfg["whale"]["min_score_lb"]


def test_base_rate_returns_the_default_below_min_n(cfg):
    res = {}
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 499, res)
    outs = whale_score.resolved_outcomes(pos, res, AS_OF)
    m0, n = whale_score.base_rate(outs, "sports", AS_OF, cfg)
    assert n == 499 and m0 == q6(cfg["whale"]["score"]["base_rate_default"])

    pos += _positions("0xw", "baseball_mlb", "WINNER", [0.08], res, tag="x")
    outs = whale_score.resolved_outcomes(pos, res, AS_OF)
    m0, n = whale_score.base_rate(outs, "sports", AS_OF, cfg)
    assert n == 500 and m0 == q6(Decimal("0.08"))


def test_base_rate_ignores_outcomes_older_than_the_lookback(cfg):
    res = {}
    old = (AS_OF - timedelta(days=40)).isoformat()
    pos = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 600, res,
                     resolved_at=old)
    outs = whale_score.resolved_outcomes(pos, res, AS_OF)
    m0, n = whale_score.base_rate(outs, "sports", AS_OF, cfg)
    assert n == 0 and m0 == q6(cfg["whale"]["score"]["base_rate_default"])


def test_a_nonsports_position_never_changes_a_sports_score(cfg):
    res = {}
    sports = _positions("0xw", "baseball_mlb", "WINNER", [0.08] * 40, res)
    alone = _score(cfg, sports, res)
    nons = _positions("0xw", "politics", "BINARY", [0.90] * 40, res)
    both = _score(cfg, sports + nons, res)
    key = ("0xw", "sports", "baseball_mlb", "baseball_mlb:WINNER")
    assert both[key] == alone[key]
    # amended 2026-09-04: the non-sports root IS the fee category, so this key
    # reads `politics`, not the pooled `nonsports` of the pre-amendment lane
    assert ("0xw", "politics", "politics", "politics:BINARY") in both
    assert all(k[1] in ("sports", "politics") for k in both)


# ---------------------------------------------------------------- hygiene

def _mm_prints():
    """A quote-stuffing maker: both outcomes of every market, $20 a print."""
    out = []
    for m, cid in enumerate(("0xm1", "0xm2")):
        for i, idx in enumerate((0, 1)):
            out.append(_print(wallet="0xmm", cid=cid, idx=idx,
                              ts=T + m * 5000 + i * 60, size="40", price="0.50"))
    return out


def test_hygiene_flags_mm_wash_and_both_sides_and_leaves_a_clean_wallet(cfg):
    prints = _mm_prints()
    prints += [_print(wallet="0xwash", cid="0xw1", ts=T, price="0.50"),
               _print(wallet="0xwash", cid="0xw1", ts=T + 1, side="SELL",
                      price="0.50")]
    prints += [_print(wallet="0xclean", cid="0xc1", ts=T),
               _print(wallet="0xclean", cid="0xc1", ts=T + 60)]
    prints += [_print(wallet="0xboth", cid="0xb1", idx=0, ts=T, size="1000"),
               _print(wallet="0xboth", cid="0xb1", idx=1, ts=T + 4000, size="500"),
               _print(wallet="0xboth", cid="0xb2", idx=0, ts=T, size="1000")]
    markets = {c: _market() for c in
               ("0xm1", "0xm2", "0xw1", "0xc1", "0xb1", "0xb2")}
    pos = whale_score.positions_from_prints(prints, markets, cfg)
    hyg = whale_hygiene.hygiene_flags(prints, pos, cfg)

    assert hyg.wallets["0xmm"] == {"mm"}
    assert "wash" in hyg.wallets["0xwash"]
    assert hyg.wallets.get("0xclean", set()) == set()
    assert hyg.wallets.get("0xboth", set()) == set()
    assert hyg.markets[("0xboth", "0xb1")] == {"both_sides"}
    assert ("0xboth", "0xb2") not in hyg.markets
    assert hyg.excludes(next(p for p in pos if p.wallet == "0xboth"
                             and p.condition_id == "0xb1"))
    assert not hyg.excludes(next(p for p in pos if p.wallet == "0xboth"
                                 and p.condition_id == "0xb2"))
    assert not hyg.excludes(next(p for p in pos if p.wallet == "0xclean"))


def test_a_token_second_side_is_within_the_both_sides_tolerance(cfg):
    prints = [_print(wallet="0xtiny", cid="0xt1", idx=0, ts=T, size="10000"),
              _print(wallet="0xtiny", cid="0xt1", idx=1, ts=T + 4000, size="500")]
    pos = whale_score.positions_from_prints(prints, {"0xt1": _market()}, cfg)
    hyg = whale_hygiene.hygiene_flags(prints, pos, cfg)
    assert hyg.markets == {}                    # 500 / 10000 = 0.05 <= 0.10


# --------------------------------------------------- non-sports roots (2026-09-04)

def test_root_of_returns_the_category_so_two_nonsports_roots_never_pool(cfg):
    """L14 non-sports amendment 2026-09-04: the root IS the Polymarket fee
    category, so `politics` and `crypto` are DISJOINT roots — one field prices
    the leg, segregates the report and isolates the score."""
    assert whale_score.root_of("politics", "BINARY") == "politics"
    assert whale_score.root_of("crypto", "LADDER") == "crypto"
    assert whale_score.root_of("geopolitics", "BINARY") == "geopolitics"
    assert whale_score.root_of("baseball_mlb", "WINNER") == "sports"
    assert whale_score.root_of(None, "BINARY") is None

    resolutions = {}
    pos = (_positions("0xw", "politics", "BINARY", [0.30], resolutions)
           + _positions("0xw", "crypto", "BINARY", [-0.30], resolutions))
    outcomes = whale_score.resolved_outcomes(pos, resolutions, AS_OF)
    agg = whale_score.aggregate(outcomes)
    assert {k[1] for k in agg} == {"politics", "crypto"}
    # no key is shared, so no sum can cross the two roots
    assert all(n == 1 for n, _ in agg.values())


def test_a_politics_edge_never_moves_the_same_wallets_sports_lower_bound(cfg):
    """Scoring isolation, stated as the operator asked it: no transfer between
    roots (operator decision 2026-09-04)."""
    rates = {"sports": Decimal("-0.02"), "politics": Decimal("-0.02")}
    resolutions = {}
    sports = _positions("0xw", "baseball_mlb", "WINNER", [0.2, 0.2, 0.2], resolutions)
    key = ("0xw", "sports", "baseball_mlb", "baseball_mlb:WINNER")
    alone = whale_score.scores(sports, resolutions, AS_OF, cfg, rates)[key]
    politics = _positions("0xw", "politics", "BINARY", [0.9] * 50, resolutions)
    both = whale_score.scores(sports + politics, resolutions, AS_OF, cfg, rates)[key]
    assert both.lb == alone.lb
    assert both.n_root == alone.n_root == 3


def test_each_root_carries_its_own_base_rate_and_starts_at_the_default(cfg):
    """A new root falls back to `base_rate_default` until it has
    `base_rate_min_n` resolved positions of ITS OWN — never the sports one."""
    resolutions = {}
    pos = (_positions("0xw", "baseball_mlb", "WINNER", [0.5] * 3, resolutions)
           + _positions("0xw", "politics", "BINARY", [0.1] * 2, resolutions))
    outcomes = whale_score.resolved_outcomes(pos, resolutions, AS_OF)
    default = q6(Decimal(str(cfg["whale"]["score"]["base_rate_default"])))
    assert whale_score.base_rate(outcomes, "politics", AS_OF, cfg) == (default, 2)
    assert whale_score.base_rate(outcomes, "sports", AS_OF, cfg) == (default, 3)


# ------------------------------------------------------------- hygiene perf
#
# Regrade 2026-09-04 (review of 7c913bf, Important #2): whale_hygiene._pairs
# is O(n^2) per (wallet, market)/(wallet, token) group and is the dominant
# cost of whale_state.score_state, which the 60s whale cycle rebuilds every
# minute under a 120s child cap. The tape is growing ~47k rows/day plus
# wallet-history backfill, so a wallet's own group size keeps climbing.
#
# `_REFERENCE_hygiene_flags` below is whale_hygiene.hygiene_flags/_pairs
# EXACTLY as they stood before the sliding-window fix (frozen here, not
# imported, so refactoring the real module can never silently rot the
# ground truth this compares against). The property test proves the fast
# path returns bit-identical flags; the timing test is the regression guard.

BUY = "BUY"


def _REFERENCE_pairs(rows, gap):
    """The old O(n^2) `_pairs`, byte-for-byte in behavior: every pair of rows
    within `gap` seconds of each other."""
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if abs(int(b["ts"]) - int(a["ts"])) <= gap:
                yield a, b


def _REFERENCE_hygiene_flags(prints, positions, cfg):
    """The old O(n^2) `hygiene_flags`, byte-for-byte in behavior."""
    h = cfg["whale"]["hygiene"]
    by_wallet = defaultdict(list)
    for p in prints:
        by_wallet[p["proxy_wallet"]].append(p)

    wallets = {}
    for wallet, rows in by_wallet.items():
        flags = set()
        by_market = defaultdict(list)
        for p in rows:
            by_market[p["condition_id"]].append(p)

        two_sided = sum(
            any(a["outcome_index"] != b["outcome_index"]
                for a, b in _REFERENCE_pairs([p for p in mrows if p["side"] == BUY],
                                             int(h["mm_window_secs"])))
            for mrows in by_market.values())
        mean_usd = sum(Decimal(str(p["notional_usd"])) for p in rows) / len(rows)
        if (Decimal(two_sided) / len(by_market) >= Decimal(h["mm_market_share"])
                and mean_usd < Decimal(str(h["mm_max_avg_usd"]))):
            flags.add("mm")

        by_token = defaultdict(list)
        for p in rows:
            by_token[(p["condition_id"], p["outcome_index"])].append(p)
        if any(a["side"] != b["side"] and Decimal(str(a["price"])) == Decimal(str(b["price"]))
               for trows in by_token.values()
               for a, b in _REFERENCE_pairs(trows, int(h["wash_gap_secs"]))):
            flags.add("wash")
        wallets[wallet] = flags

    exposure = defaultdict(lambda: defaultdict(Decimal))
    for pos in positions:
        exposure[(pos.wallet, pos.condition_id)][pos.outcome_index] += pos.size - pos.sold_size
    markets = {}
    for key, sides in exposure.items():
        live = [v for v in sides.values() if v > 0]
        if len(live) > 1 and min(live) > Decimal(h["both_sides_tolerance"]) * max(live):
            markets[key] = {"both_sides"}
    return whale_hygiene.Hygiene(wallets=wallets, markets=markets)


def _random_prints(rng, n, n_wallets=4, n_markets=3, ts_span=40):
    """Random-ish, seeded prints clustered enough (small ts_span, few
    wallets/markets/prices) that `gap`-window collisions — the case that
    actually exercises the pair scan — are common, not rare."""
    wallets = [f"0xw{i}" for i in range(n_wallets)]
    cids = [f"0xc{i}" for i in range(n_markets)]
    prices = ["0.10", "0.35", "0.50", "0.65", "0.90"]
    out = []
    for _ in range(n):
        out.append(_print(wallet=rng.choice(wallets), cid=rng.choice(cids),
                          idx=rng.choice((0, 1)), side=rng.choice(("BUY", "SELL")),
                          ts=T + rng.randint(0, ts_span),
                          price=rng.choice(prices),
                          size=rng.choice(("50", "300", "2000"))))
    return out


def test_hygiene_flags_matches_the_reference_on_random_print_sets(cfg):
    rng = random.Random(20260904)
    markets = {f"0xc{i}": _market() for i in range(3)}
    for trial in range(200):
        n = rng.randint(1, 120)
        prints = _random_prints(rng, n)
        pos = whale_score.positions_from_prints(prints, markets, cfg)
        got = whale_hygiene.hygiene_flags(prints, pos, cfg)
        want = _REFERENCE_hygiene_flags(prints, pos, cfg)
        assert got == want, f"trial {trial}, n={n}: {got} != {want}"


def test_hygiene_flags_matches_the_reference_when_the_mm_window_evicts(cfg):
    """Regrade (review of b1fc427): ts_span=40 above is far below
    mm_window_secs=600, so `_two_sided_within_window`'s eviction branch
    (`while ts_hi - ts[lo] > gap: lo += 1`) never runs in the trial above —
    every row in a group always fits in one window. ts_span=5,000 forces rows
    to fall in and out of the window, so eviction is actually exercised
    against the frozen O(n^2) reference."""
    rng = random.Random(20260904)
    markets = {f"0xc{i}": _market() for i in range(3)}
    for trial in range(200):
        n = rng.randint(1, 120)
        prints = _random_prints(rng, n, ts_span=5_000)
        pos = whale_score.positions_from_prints(prints, markets, cfg)
        got = whale_hygiene.hygiene_flags(prints, pos, cfg)
        want = _REFERENCE_hygiene_flags(prints, pos, cfg)
        assert got == want, f"trial {trial}, n={n}: {got} != {want}"


def _concentrated_prints(n, n_wallets=200, n_whales=5, n_markets=20):
    """Adversarial-but-realistic shape (regrade 2026-09-04 bench, see
    bench_hygiene.py in the report): a fixed handful of whale wallets,
    weight 8x the rest, each with 2 home markets, mostly one side/outcome —
    a real accumulating whale, and also the worst case for the old
    `any()`-short-circuited pair scan, since a match is rare so the scan
    mostly runs to the end instead of exiting early. Measured (pre-fix,
    this shape): 200k rows ~10.7s in whale_hygiene.hygiene_flags alone."""
    rng = random.Random(1)
    wallets = [f"0xw{i}" for i in range(n_wallets)]
    whales = set(wallets[:n_whales])
    weights = [8.0 if w in whales else 1.0 for w in wallets]
    cids = [f"0xc{i}" for i in range(n_markets)]
    home = {w: rng.sample(cids, k=2) for w in wallets}
    prices = ["0.10", "0.35", "0.50", "0.65", "0.90"]
    out = []
    for _ in range(n):
        w = rng.choices(wallets, weights=weights, k=1)[0]
        cid = rng.choice(home[w])
        if w in whales:
            idx, side = 0, "BUY"
        else:
            idx, side = rng.choice((0, 1)), rng.choice(("BUY", "SELL"))
        out.append(_print(wallet=w, cid=cid, idx=idx, side=side,
                          ts=T + rng.randint(0, 200_000),
                          price=rng.choice(prices), size="500"))
    return out


def test_hygiene_flags_completes_in_bounded_time_on_a_concentrated_tape(cfg):
    # Regression guard: at 200k concentrated rows the OLD O(n^2) _pairs scan
    # took >10s (measured, see .superpowers/sdd/retention-report.md). 8.0s is
    # regression headroom, not a target: measured 2.3s idle / 3.85s under CPU
    # load (regrade of b1fc427), so 3.0s flaked on a loaded machine while
    # still passing the whole fixed-scan-vs-O(n^2) comparison it exists for.
    prints = _concentrated_prints(200_000)
    markets = {f"0xc{i}": _market() for i in range(20)}
    pos = whale_score.positions_from_prints(prints, markets, cfg)
    t0 = time.perf_counter()
    whale_hygiene.hygiene_flags(prints, pos, cfg)
    dt = time.perf_counter() - t0
    assert dt < 8.0, f"hygiene_flags took {dt:.3f}s on 200k concentrated rows"


# ---------------------------------------------------------------- structure

def test_every_outcome_aggregation_key_carries_root():
    src = inspect.getsource(whale_score._agg_keys)
    for line in src.splitlines():
        if "wallet" in line and "def " not in line:
            assert "root" in line, line
    mod = (ARB / "whale_score.py").read_text()
    assert mod.count("_agg_keys(") == 2, "one definition, one call site"
    assert "groupby" not in mod
    assert mod.count("defaultdict(") == 2, "prints grouping + the one aggregate"


def test_the_module_does_no_io_and_reads_no_clock():
    mod = (ARB / "whale_score.py").read_text()
    for banned in ("sqlite3", "requests", "urllib", "datetime.now", "utcnow",
                   "time.time", "open("):
        assert banned not in mod, banned


# ---------------------------------------------------------------- frozen clock

def _build_tape(path):
    """The frozen-clock fixture as a real tape DB, through the Task 3 DDL."""
    spec = json.loads((FIX / "whale_frozen_clock.json").read_text())
    con = whale_tape.open_tape(path)
    for m in spec["markets"]:
        con.execute("INSERT INTO markets (condition_id, sport, market_type, line,"
                    " start_utc, event_key) VALUES (?,?,?,?,?,?)",
                    (m["condition_id"], m["sport"], m["market_type"], None,
                     m["start_utc"], m.get("event_key")))
    for p in spec["prints"]:
        con.execute(
            "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, condition_id,"
            " asset, outcome_index, side, price, size, notional_usd, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (spec["as_of"], p["ts"], f"0x{p['ts']}{p['proxy_wallet']}{p['side']}",
             p["proxy_wallet"], p["condition_id"], f"tok{p['outcome_index']}",
             p["outcome_index"], p["side"], p["price"], p["size"],
             str(q6(Decimal(p["price"]) * Decimal(p["size"]))), p.get("source", "tape")))
    for r in spec["resolutions"]:
        con.execute("INSERT INTO resolutions (condition_id, resolved_at,"
                    " winner_outcome_index, status) VALUES (?,?,?,?)",
                    (r["condition_id"], r["resolved_at"],
                     r["winner_outcome_index"], r["status"]))
    con.commit()
    return con, spec


def _score_tape(con, cfg, as_of):
    rows = [dict(r) for r in con.execute("SELECT * FROM prints ORDER BY id")]
    markets = {r["condition_id"]: dict(r)
               for r in con.execute("SELECT * FROM markets")}
    res = {r["condition_id"]: dict(r)
           for r in con.execute("SELECT * FROM resolutions")}
    pos = whale_score.positions_from_prints(rows, markets, cfg)
    outs = whale_score.resolved_outcomes(pos, res, as_of)
    m0, _ = whale_score.base_rate(outs, "sports", as_of, cfg)
    return whale_score.scores(pos, res, as_of, cfg, {"sports": m0})


def test_frozen_clock_equals_a_physically_truncated_tape(tmp_path, cfg):
    con, spec = _build_tape(tmp_path / "tape.sqlite")
    as_of = datetime.fromisoformat(spec["as_of"])
    full = _score_tape(con, cfg, as_of)
    con.close()

    shutil.copy(tmp_path / "tape.sqlite", tmp_path / "cut.sqlite")
    cut = sqlite3.connect(tmp_path / "cut.sqlite")
    cut.row_factory = sqlite3.Row
    gone = cut.execute("DELETE FROM resolutions WHERE resolved_at >= ?",
                       (spec["as_of"],)).rowcount
    cut.commit()
    assert gone > 0, "fixture must contain a resolution the clock has not reached"
    truncated = _score_tape(cut, cfg, as_of)
    cut.close()

    assert set(full) == set(truncated) and full
    for key, s in full.items():
        t = truncated[key]
        assert (s.mu, s.sd, s.lb, s.lb_report) == (t.mu, t.sd, t.lb, t.lb_report)
        assert (s.n_bucket, s.n_sport, s.n_root, s.native) == \
               (t.n_bucket, t.n_sport, t.n_root, t.native)
        assert all(isinstance(v, Decimal) for v in (s.mu, s.sd, s.lb, s.lb_report))


def test_a_print_with_no_outcome_index_is_skipped_not_scored_as_a_loss(cfg):
    """Minor (final review 2026-09-04): `outcomeIndex` is absent on some venue
    rows, and (wallet, cid, None) grouped into its own position that no
    resolution can ever match — `winner_outcome_index == None` is false for
    every winner, so it would have scored as a LOSS."""
    res = {}
    prints = [_print(), dict(_print(ts=T + 1), outcome_index=None)]

    pos = whale_score.positions_from_prints(prints, {"0xc": _market()}, cfg, res)

    assert [p.outcome_index for p in pos] == [0]
    assert res["no_outcome_index"] == 1
