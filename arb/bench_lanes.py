"""Lane scorers over our own ledger, the lanes that cannot be computed at all,
and the registry that names every one of them.

Each scorer returns a LaneResult whose `estimate` is the lane's pre-registered
primary, signed so that POSITIVE MEANS MONEY IN OUR FAVOUR — that is what makes
eleven heterogeneous lanes rankable on one axis.

A scorer that cannot compute its primary returns BLOCKED with the missing
prerequisite named. That is a first-class outcome: the catalog's ranked queue
already assumed several lanes were runnable that are not.
"""
from __future__ import annotations

import json
import statistics
from decimal import Decimal

from arb.bench import LaneResult, rule_of_three, verdict_for, wilson
from arb.bench_kalshi import _d, l6_tail, l8_calibration
from arb.bench_maker import l12_maker
from arb.bench_markout import l7_maker, l11_prints
from arb.models import q6

# --------------------------------------------------------------------------- #
# Lanes scored from our own persisted history
# --------------------------------------------------------------------------- #

def l1_coherence(cfg, con, hist) -> LaneResult:
    """L1 — intra-venue coherence. Snapshot prevalence of same-venue locked
    constructs that survived fees and depth, over every poll we ever ran. The
    doc is explicit that this is a CENSORED lower bound: a sub-minute violation
    that opens and closes between two polls is invisible to us, and no amount
    of stored data fixes that."""
    r = LaneResult(lane="L1", name="intra-venue coherence", unit="poll",
                   primary="prevalence of executable same-venue ARB per poll",
                   null_band=Decimal(0))   # a prevalence, not an edge per dollar
    polls = con.execute("SELECT COUNT(*) n FROM polls").fetchone()["n"]
    hit = con.execute(
        "SELECT COUNT(DISTINCT poll_id) n FROM opportunities WHERE kind='ARB'"
    ).fetchone()["n"]
    if not polls:
        r.note = "no polls persisted"
        return r
    r.n = polls
    r.estimate = q6(Decimal(hit) / polls)
    r.lo, r.hi = wilson(hit, polls, float(cfg["bench"]["ci_z"]))
    ok = con.execute("SELECT COUNT(*) n FROM opportunities "
                     "WHERE kind='ARB' AND status='OK'").fetchone()["n"]
    r.detail.append(f"{hit} polls of {polls} carried >=1 same-venue ARB; "
                    f"{ok} of those flags cleared every guard (status OK)")
    r.note = ("censored lower bound — cron cadence cannot see a violation that "
              "opens and closes between polls; prevalence, never lifetime")
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r


def _settled_bets(con) -> list[dict]:
    rows = con.execute(
        "SELECT b.*, e.payload_json FROM flat_bets b JOIN bet_events e ON e.bet_id=b.id "
        "WHERE e.kind='settled'").fetchall()
    return [dict(x) for x in rows]


def l2_pm_pm(cfg, con, hist) -> LaneResult:
    """L2 — cross-prediction-market lane. The pre-registered primary is the
    POOLED settlement-mismatch rate (per-cell rates can never reach power at
    rule-11 pair counts). A mismatch is a covering pair whose legs did not settle
    complementary: both won or both lost means the two venues graded the same
    world differently, which is fake-arb class 4 and the highest-severity one."""
    r = LaneResult(lane="L2", name="cross-venue settlement mismatch", unit="audited pair",
                   primary="pooled settlement-mismatch rate on cross-venue pairs",
                   null_band=Decimal(0))   # a rate, and a LOW one is the good news
    # The audit source is the RECONCILED event, not the settled one: locked
    # constructs settle as LOCKED_AT_ENTRY (P&L is known at entry and does not
    # depend on the outcome), so the settled payload never records how each
    # venue actually graded the world. Only `reconciled` carries per-leg venue
    # verdicts — and a leg the reconciler could not resolve is UNRESOLVABLE,
    # which is missing data, never evidence of agreement.
    # Verdicts must be UNIONED across passes, not read one event at a time:
    # reconcile is deliberately multi-pass (arb/reconcile.py:72), so a pair
    # normally appears as ["WON", null] in one event and [null, "LOST"] in a
    # later one. Reading events independently reports a fully graded pair as
    # two half-graded ones and undercounts the audit sample.
    per_bet: dict[int, dict[int, str]] = {}
    kinds: dict[int, str] = {}
    for b in con.execute(
            "SELECT b.id, b.kind, e.payload_json FROM flat_bets b JOIN bet_events e "
            "ON e.bet_id=b.id WHERE e.kind='reconciled' ORDER BY e.id"):
        v = (json.loads(b["payload_json"]) or {}).get("verdicts") or []
        if len(v) != 2:
            continue
        kinds[b["id"]] = b["kind"]
        slot = per_bet.setdefault(b["id"], {})
        for i, x in enumerate(v):
            if x is not None:
                slot[i] = x

    graded = {"resolved": 0, "partial": 0, "unresolvable": 0}
    cross = pairs = bad = same_venue = 0
    for bet_id, slot in per_bet.items():
        b = {"kind": kinds[bet_id]}
        usable = [x for x in slot.values() if x in ("WON", "LOST", "VOID")]
        if b["kind"] != "XSPREAD":
            # same-venue pairs cannot disagree ACROSS venues by construction;
            # they are counted only as a control on the audit machinery itself
            same_venue += len(usable) == 2
            continue
        cross += 1
        if len(usable) < 2:
            graded["unresolvable" if not usable else "partial"] += 1
            continue
        graded["resolved"] += 1
        pairs += 1
        if usable.count("WON") != 1:       # a covering pair pays exactly once
            bad += 1
    div = con.execute("SELECT COUNT(*) n FROM opportunities "
                      "WHERE kind='DIVERGENCE'").fetchone()["n"]
    r.detail.append(f"divergence observations available for the analysis arm: {div}")
    r.detail.append(f"cross-venue pairs reconciled: {cross} — both legs graded "
                    f"{graded['resolved']}, one leg only {graded['partial']}, "
                    f"neither {graded['unresolvable']}")
    r.detail.append(f"same-venue control pairs with both legs graded: {same_venue}")
    if not pairs:
        r.note = ("NOT a sample-size problem: of the cross-venue pairs the reconciler "
                  "has touched, none has a verdict from BOTH venues, so the "
                  "settlement-agreement audit has zero input. The lane is blocked on "
                  "the reconciler, not on time.")
        return r
    r.n = pairs
    r.estimate = q6(Decimal(bad) / pairs)
    if bad == 0:
        r.lo, r.hi = Decimal(0), rule_of_three(pairs)
        r.detail.append(f"0/{pairs} mismatches; rule-of-three upper bound "
                        f"{float(r.hi):.4f}")
    else:
        r.lo, r.hi = wilson(bad, pairs, float(cfg["bench"]["ci_z"]))
    r.note = ("pooled by design; per venue-pair x market-type cells can never "
              "reach power at rule-11 throttled pair counts")
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r


def l4_value(cfg, con, hist) -> LaneResult:
    """L4 — settlement-graded value. Realized after-fee return per dollar staked
    across every bet the ledger has actually settled. No CLV: measurement rule
    on L4 is explicit that a cron-cadence 'close' is hours stale with error
    correlated to our own cadence, which is the confounder the lane exists to
    avoid."""
    from arb.metrics import bet_pnl_optimistic

    class _S:                                  # metrics wants a Store-like reader
        def query(self, sql, params=()):
            return con.execute(sql, params).fetchall()

    r = LaneResult(lane="L4", name="settlement-graded value", unit="settled bet",
                   primary="pooled after-fee return per dollar staked")
    rets, seen = [], set()
    for b in _settled_bets(con):
        if b["id"] in seen:
            continue
        seen.add(b["id"])
        pnl = bet_pnl_optimistic(_S(), b)
        stake = _d(b["stake_usd"])
        if pnl is None or not stake:
            continue
        rets.append(float(pnl / stake))
    if not rets:
        r.note = "no settled bet with a derivable P&L"
        return r
    r.n = len(rets)
    mean = statistics.fmean(rets)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0.0
    half = float(cfg["bench"]["ci_z"]) * sd / (r.n ** 0.5)
    r.estimate, r.lo, r.hi = (q6(Decimal(mean)), q6(Decimal(mean - half)),
                              q6(Decimal(mean + half)))
    need = int(cfg["bench"]["n_min_units"])
    r.detail.append(f"{r.n} settled bets of the {need} the pre-registered primary "
                    f"needs ({r.n / need:.0%} of decision-grade)")
    r.note = ("composition is whatever the ledger settled — overwhelmingly REVIEW-lane "
              "cross-venue pairs, NOT the pinned-benchmark universe L4 pre-registers; "
              "read as a ledger health check until the benchmark book list is verified in-feed")
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r


# --------------------------------------------------------------------------- #
# Lanes that cannot be computed — each names its missing prerequisite
# --------------------------------------------------------------------------- #

def _blocked(lane: str, name: str, primary: str, why: str) -> LaneResult:
    return LaneResult(lane=lane, name=name, primary=primary, note=why)


def l3_book(cfg, con, hist) -> LaneResult:
    """L3 — PM<->book settlement forensics. Still BLOCKED, but no longer vague:
    the missing input is a per-book settlement-rules library, and this reports
    exactly how much of it exists.

    The cells are deliberately unfilled rather than defaulted. A guessed
    convention would yield a mismatch rate that looks measured and is invented,
    which is fake-arb class 4 manufactured by our own instrument — the single
    failure this lane exists to detect."""
    from pathlib import Path

    import yaml

    r = LaneResult(lane="L3", name="PM<->book forensics", unit="book x rule",
                   primary="pooled settlement-mismatch rate vs book conventions")
    path = Path("data/book_rules.yaml")
    if not path.exists():
        r.note = "data/book_rules.yaml missing — the rules-library scaffold is gone"
        return r
    lib = yaml.safe_load(path.read_text()) or {}
    dims = list((lib.get("dimensions") or {}).keys())
    books = lib.get("books") or {}
    filled = sum(1 for b in books.values() if b.get("source_url")
                 for d in dims if b.get(d) is not None)
    total = len(books) * len(dims)
    r.n = filled
    r.detail.append(f"rules library: {filled}/{total} cells verified "
                    f"({len(books)} books in our feed x {len(dims)} dimensions: "
                    f"{', '.join(dims)})")
    r.detail.append(f"consensus rows already stored for the staleness arm: "
                    f"{con.execute('SELECT COUNT(*) n FROM consensus').fetchone()['n']}")
    r.detail.append("unfilled books: " + ", ".join(
        sorted(k for k, v in books.items() if not v.get("source_url")) ) or "none")
    if filled < total:
        r.note = (f"BLOCKED on hand work, not on code or data volume: {total - filled} "
                  f"of {total} rule cells are unread. Each needs one look at the book's "
                  f"own rulebook (data/book_rules.yaml states the enum per dimension). "
                  f"Cells are left null on purpose — a guessed convention would "
                  f"fabricate the very mismatch class this lane measures.")
        return r
    r.note = ("rules library complete — the mismatch diff against "
              "data/settlement_matrix.yaml can now run")
    return r


def l5_staleness(cfg, con, hist) -> LaneResult:
    """L5 — pre-match staleness. The lane has two arms and they are not equally
    blocked, which the catalog's single BLOCKED verdict hid:

      venue<->venue  computable NOW from stored quotes. arb/leadlag.py already
                     does exactly this study — matched moves, first-mover tally,
                     cluster-bootstrap CI over event_key — so the lane needs no
                     new collector, only to be pointed at the data we hold.
      venue<->book   still blocked. Identifying a lag against the book feed
                     needs sub-poll sampling we do not have, and Odds API
                     historical snapshots are paid-plan-only.

    Primary is the first-mover excess: p_lead - 0.5, signed so positive means
    the pair leads in OUR favour (a venue we can see moving first is a lag we
    could in principle price). Anything inside the ties band is unidentifiable
    at cron cadence, which the leadlag module's tie_epsilon already encodes."""
    r = LaneResult(lane="L5", name="pre-match staleness", unit="matched move",
                   primary="|p_lead - 0.5|: magnitude of identifiable first-mover "
                           "asymmetry between venues",
                   null_band=Decimal(0))   # a probability excess, not money

    class _S:                                  # leadlag wants a Store-like reader
        path = None

        def query(self, sql, params=()):
            return con.execute(sql, params).fetchall()

        def insert(self, table, row):
            """run_study appends its run to leadlag_runs; the bench is read-only
            everywhere, so the write is dropped. `arb leadlag` remains the only
            command that persists a canonical, parameter-stamped run."""
            return 0

    from arb.leadlag import run_study
    res = run_study(cfg, _S())
    gate_moves = int(cfg["leadlag"]["min_matched_moves"])
    gate_events = int(cfg["leadlag"]["min_events"])
    best = None
    for pair, p in sorted(res["pairs"].items()):
        line = (f"{pair}: matched={p['n_matched']} ties={p['n_ties']} "
                f"events={p['n_events']} p_lead={p['p_lead_first']:.4f} "
                f"CI[{p['ci_low']:.4f},{p['ci_high']:.4f}] leader={p['leader'] or 'none'} "
                f"median_lead={p['median_lead_secs']:.2f}s")
        if p["n_matched"] < gate_moves or p["n_events"] < gate_events:
            line += "   <- below pre-registered gate, no claim"
        elif best is None or p["n_matched"] > best[1]["n_matched"]:
            best = (pair, p)
        r.detail.append(line)
    r.detail.append(f"excluded for no bid (mid undefined): "
                    f"{res['excluded_no_bid_fraction']:.2%}")
    r.detail.append("venue<->book arm remains BLOCKED: sub-poll sampling absent, "
                    "Odds API historical is paid-plan-only at 10 credits/region/market")
    if best is None:
        r.note = ("no venue pair cleared the pre-registered gates "
                  f"({gate_moves} matched moves, {gate_events} events)")
        return r
    pair, p = best
    r.n = p["n_matched"]
    # p_lead is "did the alphabetically-first venue move first" — a direction,
    # not a P&L. The lane's question is whether ANY identifiable asymmetry
    # exists (one venue systematically trailing IS the staleness this lane
    # measures), so the primary is its MAGNITUDE, folded to the positive side.
    # The fold is monotone, so the CI folds with it; a CI straddling 0.5 leaves
    # the asymmetry unidentified and is passed through unfolded to span zero.
    lead, lo_p, hi_p = (Decimal(str(p[k])) for k in
                        ("p_lead_first", "ci_low", "ci_high"))
    half = Decimal("0.5")
    if hi_p < half:                     # the OTHER venue leads: mirror the axis
        r.estimate, r.lo, r.hi = q6(half - lead), q6(half - hi_p), q6(half - lo_p)
    elif lo_p > half:
        r.estimate, r.lo, r.hi = q6(lead - half), q6(lo_p - half), q6(hi_p - half)
    else:                               # straddles: no identified leader
        r.estimate, r.lo, r.hi = q6(lead - half), q6(lo_p - half), q6(hi_p - half)
    r.note = (f"{pair}, leader={p['leader']}, median lead "
              f"{p['median_lead_secs']:.1f}s. {p['n_ties']} of {p['n_matched']} matched moves are TIES "
              f"inside one poll cycle — at cron cadence the fetch ORDER stamps the "
              f"earlier time, not market leadership, so the tie mass is censoring, "
              f"not evidence of simultaneity")
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r


def _overround(bookmaker: dict, market_key: str) -> float | None:
    """Sum of implied probabilities across a complete h2h market. 1.06 means the
    book holds 6 points of vig. Incomplete markets are dropped, not padded — a
    two-of-three-way market would read as a fake bargain."""
    for mk in bookmaker.get("markets", []):
        if mk.get("key") != market_key:
            continue
        prices = [o.get("price") for o in mk.get("outcomes", [])]
        if len(prices) < 2 or any(not p for p in prices):
            return None
        return sum(1.0 / float(p) for p in prices)
    return None


def l9_anj(cfg, con, hist) -> LaneResult:
    """L9 — ANJ overround telemetry. The vig gap between the five ANJ-licensed
    French books and the sharp reference in the same call, which is what sets
    how large a divergence must be before anything ANJ-side could be +EV.
    Signed as a cost: negative means ANJ charges more vig than the reference."""
    b = cfg["bench"]
    r = LaneResult(lane="L9", name="ANJ overround telemetry", unit="event x book",
                   primary="sharp-minus-ANJ overround (negative = extra ANJ vig)",
                   null_band=Decimal(0))   # overround points, not edge per dollar
    from pathlib import Path
    path = Path(str(b["anj_probe_file"]))
    if not path.exists():
        r.note = (f"needs one live Odds API call over regions "
                  f"'{b['anj_probe_region']},{cfg['consensus_anchor']['region']}' "
                  f"(2 credits). Not spent automatically — run `arb bench --probe-anj`.")
        return r
    probe = json.loads(path.read_text())
    anj, sharp = [], []
    for ev in probe.get("events", []):
        for bk in ev.get("bookmakers", []):
            o = _overround(bk, str(b["anj_probe_markets"]))
            if o is None:
                continue
            (anj if bk.get("key", "").endswith("_fr") else sharp).append(o)
    if not anj or not sharp:
        r.note = (f"probe returned {len(anj)} ANJ and {len(sharp)} reference books — "
                  f"need both sides to state a gap")
        return r
    r.n = len(anj) + len(sharp)
    gap = statistics.fmean(sharp) - statistics.fmean(anj)
    sd = (statistics.pstdev(anj + sharp) or 0.0)
    half = float(b["ci_z"]) * sd / (min(len(anj), len(sharp)) ** 0.5)
    r.estimate, r.lo, r.hi = (q6(Decimal(gap)), q6(Decimal(gap - half)),
                              q6(Decimal(gap + half)))
    r.detail.append(f"ANJ books n={len(anj)} mean overround "
                    f"{statistics.fmean(anj):.4f}; reference n={len(sharp)} mean "
                    f"{statistics.fmean(sharp):.4f}")
    r.detail.append(f"probe: {probe.get('sport')} {probe.get('fetched_at')} "
                    f"regions={probe.get('regions')} credits={probe.get('credits_spent')}")
    r.note = ("descriptive telemetry, not an edge: this lane exists to calibrate how "
              "big a divergence must be, and France is data-only in this stack (§0)")
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r


def l10_dispute(cfg, con, hist) -> LaneResult:
    return _blocked("L10", "dispute case-log", "price path through a dispute window",
                    "no dispute-detection feed exists; this is a passive case-series that "
                    "accrues only when a market we already persist enters a dispute.")




LANES = {
    "L1": ("intra-venue coherence", l1_coherence),
    "L2": ("cross-venue settlement mismatch", l2_pm_pm),
    "L3": ("PM<->book forensics", l3_book),
    "L4": ("settlement-graded value", l4_value),
    "L5": ("pre-match staleness", l5_staleness),
    "L6": ("near-certainty tail rate", l6_tail),
    "L7": ("maker bounds (Kalshi)", l7_maker),
    "L8": ("calibration harvests", l8_calibration),
    "L9": ("ANJ overround telemetry", l9_anj),
    "L10": ("dispute case-log", l10_dispute),
    "L11": ("large-print markout", l11_prints),
    "L12": ("maker bracket (PM WS tape)", l12_maker),
}

