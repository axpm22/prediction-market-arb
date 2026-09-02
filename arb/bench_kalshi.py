"""Retrospective Kalshi lanes (L6, L7, L8) and the candle helpers they share.

These are the only lanes that reach decision-grade power today: the corpus is
free, keyless and already settled, so their n is bounded by how many requests we
are willing to make rather than by how long we are willing to wait.

L6 and L8 share an identity worth stating once: for a contract bought at all-in
cost c and settled 0/1, expected profit per contract is

    E[profit] = P(win) x (1 - c) - (1 - P(win)) x c = P(win) - c

so "realized frequency minus mean all-in cost" IS the after-fee edge per dollar
of notional. Both primaries are that number, which is why their CIs are just a
Wilson interval on the frequency shifted by the mean cost — and why they are
directly comparable with every other lane's estimate.
"""
from __future__ import annotations

import json
import statistics
from decimal import Decimal, InvalidOperation

from arb.bench import LaneResult, rule_of_three, verdict_for, wilson
from arb.models import q6
from arb.venues.fees import leg_fee_cost

# Kalshi category -> the fee category key our fee table understands. Kalshi's
# own labels are title-cased and coarser than Polymarket's; only the mapping
# matters, the rate is read from config (rule 4).
KALSHI_FEE_CATEGORY = {
    "Sports": "sports", "Politics": "politics", "Elections": "politics",
    "Economics": "econ", "Financials": "politics", "Climate and Weather": "weather",
    "Companies": "politics", "Science and Technology": "tech",
    "Entertainment": "culture", "Health": "other", "World": "geopolitics",
}


def _d(v) -> Decimal | None:
    try:
        return None if v in (None, "") else Decimal(str(v))
    except (InvalidOperation, TypeError):
        return None


def _candles(row) -> list[dict]:
    return json.loads(row["candles_json"] or "[]")


def _two_sided(cfg, candle: dict, ask: Decimal) -> bool:
    """A quoted ask is only a PRICE if something stands behind it. A freshly
    opened Kalshi market quotes 0.97 ask / 0.01 bid on zero volume — read
    naively that is a 97%-certain contract, and it is nothing of the kind.
    Six of the first seven markets this bench put in the >=97c cohort were
    exactly that (observed 2026-07-27), which is fake-arb class 3/8: a
    present-but-untradeable price. Requiring a live bid within a bounded
    spread is the cheapest filter that kills the whole family."""
    bid = _d((candle.get("yes_bid") or {}).get("close_dollars"))
    return (bid is not None and bid > 0
            and (ask - bid) <= Decimal(str(cfg["bench"]["max_spread"])))


def _ask_at(cfg, candles: list[dict], cutoff_ts: int,
            side: str = "yes") -> Decimal | None:
    """Last EXECUTABLE ask for `side` at or before `cutoff_ts`. An ask of exactly
    1.00 with no offer behind it is the empty-book trap (arb/venues/kalshi.py:56),
    not a price; 0.00 is likewise not buyable; and a one-sided book is handled
    by _two_sided. All are dropped rather than winsorized — measurement rule 1
    says executable prices only.

    side="no" prices the OTHER side of the same candle. The candles carry only
    `yes_ask`/`yes_bid`, so the no-ask comes from the identity verified in
    data/verified_facts.md and arb/venues/kalshi.py: the ask of one side is
    1 - the opposite side's best bid. Inverting that silently flips the whole
    lane, which is rule 10's trap in Kalshi form — tests/test_no_side_calibration.py
    pins it.

    The validation gate is deliberately the YES gate for BOTH sides, so the two
    lanes select an identical population and stay comparable market-for-market.
    """
    best = None
    for c in candles:
        if c.get("end_period_ts", 0) > cutoff_ts:
            break                      # candles arrive in ascending period order
        a = _d((c.get("yes_ask") or {}).get("close_dollars"))
        if a is not None and Decimal(0) < a < Decimal(1) and _two_sided(cfg, c, a):
            best = (a if side == "yes"
                    else Decimal(1) - _d((c.get("yes_bid") or {}).get("close_dollars")))
    return best


def _all_in(cfg, category: str, ask: Decimal) -> Decimal:
    """Ask plus the taker fee we would actually pay. The fee is what turns a
    calibration curve into an edge curve, and at 97c it is a third of the gross
    (STRATEGIES.md B0) — never omit it."""
    cat = KALSHI_FEE_CATEGORY.get(category, "other")
    return q6(ask + leg_fee_cost(cfg, "kalshi", cat, ask, Decimal(1), "paper", "taker"))


def _freq_minus_cost(cfg, obs: list[tuple[Decimal, int, str]]) -> tuple:
    """obs = [(all_in_cost, won, cluster_key)]. Returns (edge, lo, hi, n, k, nc).

    The CI uses the number of CLUSTERS, not observations: both sides of one
    Kalshi event are the same coin flip seen twice, and rule 11 forbids letting
    that inflate n. ponytail: conservative shrink-n, not a cluster-robust
    sandwich estimator — upgrade if a lane's verdict ever turns on it."""
    if not obs:
        return None, None, None, 0, 0, 0
    n = len(obs)
    k = sum(w for _, w, _ in obs)
    nc = len({key for _, _, key in obs}) or n
    mean_cost = q6(sum(c for c, _, _ in obs) / n)
    lo, hi = wilson(round(k / n * nc), nc, float(cfg["bench"]["ci_z"]))
    return q6(Decimal(k) / n - mean_cost), q6(lo - mean_cost), q6(hi - mean_cost), n, k, nc


# --------------------------------------------------------------------------- #
# Retrospective Kalshi lanes (zero credits, decision-grade today)
# --------------------------------------------------------------------------- #

def _horizon_observations(cfg, hist, dropped: dict | None = None,
                          side: str = "yes") -> list[dict]:
    """One observation per settled market: the last executable ask `horizon_hours`
    before close, its all-in cost, and the outcome. This is the pre-registered
    snapshot rule of measurement rule 11 — one unit per market, never per poll.

    `dropped` accumulates why markets fell out. Selection this aggressive has to
    be visible: a lane that silently keeps 5% of its corpus is reporting on a
    universe nobody chose, which is measurement rule 15's coverage mask applied
    to a retrospective sample instead of a live series."""
    hours = int(cfg["bench"]["horizon_hours"])
    seen = dropped if dropped is not None else {}
    seen.setdefault("too_short", 0)
    seen.setdefault("no_executable_ask", 0)
    out = []
    for row in hist.execute(
            "SELECT m.ticker, m.event_ticker, m.category, m.result, m.close_time, "
            "       c.candles_json FROM hist_markets m JOIN hist_candles c USING (ticker) "
            "WHERE m.result IN ('yes','no')"):
        candles = _candles(row)
        if not candles:
            continue
        close_ts = max(c.get("end_period_ts", 0) for c in candles)
        cutoff = close_ts - hours * 3600
        if min(c.get("end_period_ts", 0) for c in candles) > cutoff:
            seen["too_short"] += 1      # market did not exist that far before close
            continue
        ask = _ask_at(cfg, candles, cutoff, side)
        if ask is None:
            seen["no_executable_ask"] += 1   # one-sided or empty book at the horizon
            continue
        out.append({"ticker": row["ticker"], "event": row["event_ticker"],
                    "category": row["category"] or "Uncategorized",
                    "cost": _all_in(cfg, row["category"] or "", ask), "ask": ask,
                    # `side` is "yes"/"no" and so is `result`: the no-buyer
                    # WINS the markets the yes-buyer loses.
                    "won": 1 if row["result"] == side else 0, "candles": candles})
    return out


def l8_calibration(cfg, con, hist) -> LaneResult:
    """L8 — after-fee EXECUTABLE edge across the Kalshi settled history.

    Read the name with care. Measurement rule 1 requires executable prices, so
    this prices at the ASK, which means the estimate bundles three things:
    true calibration error, the half-spread we cross, and the taker fee. It
    cannot isolate a calibration gap — that would need mid prices, which rule 1
    bans outright. What it measures is what a TAKER actually earns.

    That distinction is not academic. At 35k candles every band came back <= 0
    with favourites worst (-0.0949 in [0.70,0.90)), which reads as a
    favourite-longshot bias only if you squint at one end. Both ends negative
    is the signature of paying spread+fee on every trade, not of a directional
    mispricing. Rule 18 already says a taker-style result bounds taker
    implementations only; this lane is the reason that rule exists.
    Primary cell is pre-registered (`bench.primary_band`): the longshot band,
    where the favorite-longshot literature puts the effect. Every other band is
    exploratory (rule 12) and printed as detail, never promoted to the headline.
    """
    r = LaneResult(lane="L8", name="calibration harvests",
                   primary=f"after-fee TAKER edge (freq - ask - fee) in band "
                           f"{cfg['bench']['primary_band']}, unit = market",
                   unit="market")
    if hist is None:
        r.note = "no Kalshi corpus: run `arb bench --pull` (keyless, 0 credits)"
        return r
    dropped = {}
    obs = _horizon_observations(cfg, hist, dropped)
    if not obs:
        r.note = ("corpus holds no market with an executable ask "
                  f"{cfg['bench']['horizon_hours']}h before close")
        return r
    _band_scan(cfg, obs, dropped, r)
    return r


def _band_scan(cfg, obs: list[dict], dropped: dict, r: LaneResult) -> None:
    """Score every pre-registered price band and fill `r` in place.

    Shared by L8 (yes side) and L13 (no side) so the multiple-comparison
    bookkeeping lives once: if the two lanes ever disagreed about what counts
    as a band, a cleared CI, or a family-wise correction, they would stop being
    comparable and the whole point of running both would be lost."""
    bands = [float(b) for b in cfg["bench"]["price_bands"]]
    plo, phi = (float(x) for x in cfg["bench"]["primary_band"])
    flagged = 0
    for i in range(len(bands) - 1):
        lo_b, hi_b = bands[i], bands[i + 1]
        cell = [(o["cost"], o["won"], o["event"]) for o in obs
                if lo_b <= float(o["cost"]) < hi_b]
        est, lo, hi, n, k, nc = _freq_minus_cost(cfg, cell)
        if n == 0:
            continue
        line = (f"band [{lo_b:.2f},{hi_b:.2f}): n={n:>5} clusters={nc:>5} "
                f"won={k / n:.4f} edge={float(est):+.4f} "
                f"CI[{float(lo):+.4f},{float(hi):+.4f}]")
        if (lo_b, hi_b) == (plo, phi):
            r.estimate, r.lo, r.hi, r.n = est, lo, hi, n
            r.note = (f"{nc} independent events behind {n} markets; "
                      f"exploration sample — rule 12 confirms out-of-sample only")
            line += "   <- PRIMARY"
        elif float(lo) > 0 or float(hi) < 0:
            line += "   <- exploratory, CI excludes 0"
            flagged += 1
        r.detail.append(line)
    if flagged:
        # Measurement rule 12: a grid cell that clears its own CI has not cleared
        # the family. Six bands searched means the naive 5% is nearer 30%.
        r.detail.append(
            f"{flagged} exploratory band(s) cleared an uncorrected CI. With "
            f"{len(bands) - 1} bands searched the family-wise error is ~"
            f"{1 - 0.95 ** (len(bands) - 1):.0%} — these are hypotheses for the "
            f"prospective confirmation sample, NOT results")
    total = len(obs) + sum(dropped.values())
    r.detail.insert(0, f"corpus: {len(obs)} of {total} settled markets kept "
                       f"({len(obs) / total:.0%}) — dropped "
                       f"{dropped['too_short']} shorter-lived than the "
                       f"T-{cfg['bench']['horizon_hours']}h horizon, "
                       f"{dropped['no_executable_ask']} with no two-sided book there")
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)


def l13_no_side(cfg, con, hist) -> LaneResult:
    """L13 — the SAME books L8 prices, bought from the other side.

    L8 reads `yes_ask` and only `yes_ask`. Every band it reports is the return
    to a taker who bought YES, so its uniformly negative result is consistent
    with two very different worlds: an efficient market where we pay the
    spread, or a real mispricing we were standing on the wrong side of. L8
    cannot separate them, because it never prices the other side.

    The favorite-longshot literature (Kalshi microstructure work, 2026) reports
    the bias is largely a *Yes Bias* — YES systematically overpriced — which
    makes buying NO the mirror trade.

    READ THE RESULT CAREFULLY. This is NOT independent evidence. Buying both
    sides of one market loses exactly the spread plus both fees, so
    yes_edge + no_edge == -(spread + fees) identically, market by market
    (pinned in tests/test_no_side_calibration.py). A positive number here is
    the arithmetic mirror of L8's negative one, not a second observation of it.
    What is genuinely new is only the DIRECTION: whether either side of these
    books was payable at all, which L8's design could not ask.

    Same horizon, same two-sided gate, same bands, same clustering, same
    family-wise correction — so the two lanes are comparable market-for-market.
    """
    r = LaneResult(lane="L13", name="no-side calibration (Yes-Bias test)",
                   primary=f"after-fee TAKER edge buying NO (freq - no_ask - fee) "
                           f"in band {cfg['bench']['primary_band']}, unit = market",
                   unit="market")
    if hist is None:
        r.note = "no Kalshi corpus: run `arb bench --pull` (keyless, 0 credits)"
        return r
    dropped = {}
    obs = _horizon_observations(cfg, hist, dropped, side="no")
    if not obs:
        r.note = ("corpus holds no market with an executable no-ask "
                  f"{cfg['bench']['horizon_hours']}h before close")
        return r
    _band_scan(cfg, obs, dropped, r)
    r.detail.append(
        "MIRROR, NOT REPLICATION: yes_edge + no_edge == -(spread + fees) by "
        "construction, so this lane adds a direction, never a second sample")
    return r


def l6_tail(cfg, con, hist) -> LaneResult:
    """L6 — near-certainty tail rate. Cohort entry is EXECUTABLE, not observed:
    the ask (what a buyer crosses) must reach the band, not the last trade. The
    doc's P(>=97c -> settles 0) is reported in detail; the ranked primary is the
    same cohort's after-fee edge, so it is comparable with every other lane."""
    band = Decimal(str(cfg["bench"]["tail_band"]))
    r = LaneResult(lane="L6", name="near-certainty tail rate",
                   primary=f"after-fee edge of the executable >={band} cohort, "
                           f"unit = market",
                   unit="market")
    if hist is None:
        r.note = "no Kalshi corpus: run `arb bench --pull` (keyless, 0 credits)"
        return r
    cohort = []
    for row in hist.execute(
            "SELECT m.ticker, m.event_ticker, m.category, m.result, c.candles_json "
            "FROM hist_markets m JOIN hist_candles c USING (ticker) "
            "WHERE m.result IN ('yes','no')"):
        entry = None
        for c in _candles(row):
            a = _d((c.get("yes_ask") or {}).get("low_dollars"))
            traded = _d((c.get("price") or {}).get("high_dollars"))
            # STRATEGIES.md L6 says "markets that ever TRADED >= 97c". The trade
            # requirement is not decoration: without it the cohort fills with
            # opening one-sided books (see _two_sided).
            if (a is not None and band <= a < Decimal(1)
                    and traded is not None and traded >= band
                    and _two_sided(cfg, c, a)):
                entry = a               # first hour the cohort price was buyable
                break
        if entry is None:
            continue
        cohort.append((_all_in(cfg, row["category"] or "", entry),
                       1 if row["result"] == "yes" else 0, row["event_ticker"]))
    est, lo, hi, n, k, nc = _freq_minus_cost(cfg, cohort)
    r.estimate, r.lo, r.hi, r.n = est, lo, hi, n
    if n:
        tails = n - k
        bound = (rule_of_three(nc) if tails == 0
                 else wilson(round(tails / n * nc), nc, float(cfg["bench"]["ci_z"]))[1])
        r.detail.append(f"cohort n={n} markets / {nc} events; "
                        f"tail events (bought >={band}, settled NO) = {tails}")
        r.detail.append(f"P(tail) = {tails / n:.5f}, 95% upper bound "
                        f"{float(bound):.5f}"
                        + ("  [rule of three: zero events observed]" if tails == 0 else ""))
        r.note = ("mixed-source cohort: named-source and UMA-style resolutions are "
                  "NOT separated here (STRATEGIES.md L6 requires separate arms)")
    else:
        r.note = f"no market ever offered an executable ask >= {band}"
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r
