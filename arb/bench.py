"""Lane bench: measure every surviving STRATEGIES.md lane on data we actually
hold, then rank them by decision-grade signal per credit.

The catalog's §4 queue is an *a priori* ranking — it orders lanes by what the
audit expected them to yield. This module produces the *measured* ranking: each
lane's pre-registered primary is computed from real data, given a confidence
interval, and assigned a verdict. A lane that cannot be computed says so, with
the reason, instead of silently scoring zero.

Read-only everywhere. The ledger DB is opened `mode=ro` (rule 9 cannot even be
approached), the Kalshi corpus is a cache built by arb/kalshi_history.py, and
no Odds API credit is spent unless a scorer's credit line item is non-zero and
the operator passed --probe (rule 2).

Verdict ladder, pre-registered here and applied identically to every lane
(measurement rule 10 — kill criteria declared before the data):

  SIGNAL        the CI excludes zero: the lane measures something real
  NULL          the CI lies wholly inside +/- null_band: decisively nothing
                there. This is a *result*, not a failure — it retires the lane.
  UNDERPOWERED  the CI spans both: more data would change the answer
  BLOCKED       the lane cannot be computed at all; `note` says what is missing

Ranking is by verdict first, then by decisiveness-per-credit:

  z     = |estimate| / CI half-width     (how far from noise the answer is)
  score = z / (1 + credits_per_month/100)

so a zero-credit lane that answers cleanly outranks a credit-hungry one with
the same z — which is measurement rule 6's "finding per credit" made concrete.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal

from arb.models import q6

VERDICT_RANK = {"SIGNAL": 0, "NULL": 1, "UNDERPOWERED": 2, "BLOCKED": 3}


@dataclass
class LaneResult:
    lane: str                       # "L8"
    name: str
    primary: str                    # the pre-registered primary, in words
    unit: str = "-"                 # rule 11: the independent unit of analysis
    n: int = 0                      # independent units available TODAY
    estimate: Decimal | None = None
    lo: Decimal | None = None
    hi: Decimal | None = None
    verdict: str = "BLOCKED"
    credits: int = 0                # Odds API credits/month (B1 line item)
    note: str = ""
    null_band: Decimal | None = None   # economic floor for THIS lane's units;
                                       #   None = use the config default
    detail: list[str] = field(default_factory=list)   # exploratory, rule 12

    @property
    def z(self) -> float:
        """Decisiveness: estimate over CI half-width. Zero when unmeasured."""
        if self.estimate is None or self.lo is None or self.hi is None:
            return 0.0
        half = (float(self.hi) - float(self.lo)) / 2
        return abs(float(self.estimate)) / half if half > 0 else 0.0

    @property
    def score(self) -> float:
        return self.z / (1 + self.credits / 100)

    @property
    def favourable(self) -> bool:
        """Sign of the primary, under the module-wide convention that positive
        means money in our favour. A decisive answer and a live strategy are
        not the same result: L7's markout is a clean SIGNAL that says makers
        LOSE, and burying that in a sign character would read as a win."""
        return self.estimate is not None and self.estimate > 0

    @property
    def label(self) -> str:
        if self.verdict != "SIGNAL":
            return self.verdict
        return "SIGNAL favourable" if self.favourable else "SIGNAL adverse"


def wilson(k: int, n: int, z: float) -> tuple[Decimal, Decimal]:
    """Wilson score interval for a proportion. Chosen over the normal
    approximation because every interesting cell here is near 0 or near 1
    (longshot bands, 97c tails) where the normal interval runs outside [0,1]
    and understates the tail — the exact failure measurement rule 10 warns
    about when it says skew-heavy cells kill on calibration CIs."""
    if n <= 0:
        return Decimal(0), Decimal(1)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return q6(Decimal(max(0.0, centre - half))), q6(Decimal(min(1.0, centre + half)))


def rule_of_three(n: int) -> Decimal:
    """Upper 95% bound on a rate after n trials with zero events. The only
    honest statement a rare-event lane can make before its first event."""
    return q6(Decimal(3) / Decimal(n)) if n > 0 else Decimal(1)


def verdict_for(cfg, est: Decimal | None, lo: Decimal | None,
                hi: Decimal | None, n: int, null_band=None) -> str:
    """The ladder in the module docstring.

    NULL is tested BEFORE signal, and that order is the whole point: a CI that
    excludes zero but lies wholly inside the band describes an effect that is
    real and too small to act on. Ranking that as SIGNAL would promote 0.38c
    per dollar — L11's measured markout — to the same tier as a live edge.
    Testing NULL first can only demote, never manufacture, a positive.

    `null_band` is denominated in the LANE'S OWN units. The config default is
    money per dollar staked, which is meaningless for a prevalence (L1) or a
    first-mover excess (L5); those lanes pass 0, so zero-exclusion alone
    decides them and they can never be called economically negligible on a
    scale that does not apply to them."""
    if est is None or lo is None or hi is None or n <= 0:
        return "BLOCKED"
    band = float(cfg["bench"]["null_band"] if null_band is None else null_band)
    if max(abs(float(lo)), abs(float(hi))) <= band:
        return "NULL"
    if float(lo) > 0 or float(hi) < 0:
        return "SIGNAL"
    return "UNDERPOWERED"


def rank(results: list[LaneResult]) -> list[LaneResult]:
    # favourable before adverse WITHIN a verdict tier: both are decisive, but
    # only one describes a lane worth pursuing
    return sorted(results, key=lambda r: (VERDICT_RANK[r.verdict],
                                          0 if r.favourable else 1,
                                          -r.score, r.credits))


def _fmt(v: Decimal | None) -> str:
    return "-" if v is None else f"{float(v):+.4f}"


def render(results: list[LaneResult], n_min: int) -> str:
    """Plain text table — the bench's whole output surface. No file is written;
    callers redirect if they want one."""
    ranked = rank(results)
    w = max(len(r.name) for r in ranked)
    head = (f"{'#':>2}  {'lane':<4} {'name':<{w}}  {'unit':<10} {'n':>7}  "
            f"{'estimate':>9}  {'95% CI':>21}  {'z':>5}  {'cr/mo':>5}  verdict")
    lines = [head, "-" * len(head)]
    for i, r in enumerate(ranked, 1):
        ci = f"[{_fmt(r.lo)}, {_fmt(r.hi)}]" if r.lo is not None else "-"
        lines.append(
            f"{i:>2}  {r.lane:<4} {r.name:<{w}}  {r.unit:<10} {r.n:>7}  "
            f"{_fmt(r.estimate):>9}  {ci:>21}  {r.z:>5.1f}  {r.credits:>5}  {r.label}")
    lines.append("")
    lines.append(f"decision-grade standard: {n_min} independent units per lane")
    lines.append("rank = verdict, then favourable-before-adverse, "
                 "then z/(1+credits/100)  [decisiveness per credit]")
    lines.append("estimate is signed: POSITIVE = money in our favour, "
                 "NEGATIVE = a measured cost")
    lines.append("")
    for r in ranked:
        lines.append(f"{r.lane} — {r.name}")
        lines.append(f"     primary: {r.primary}")
        if r.note:
            lines.append(f"     note:    {r.note}")
        for d in r.detail:
            lines.append(f"     {d}")
        lines.append("")
    return "\n".join(lines)


def persist(store, results: list[LaneResult], ran_at: str) -> None:
    """Append this run to `bench_results` (schema v8) so the dashboard can show
    the latest verdict per lane without re-running the bench. INSERT-only, one
    row per lane, never an update of a previous run — the history IS the point:
    a lane that flipped verdict shows two rows, not one overwritten one."""
    for r in results:
        store.insert("bench_results", {
            "ran_at": ran_at, "lane": r.lane,
            "primary_value": None if r.estimate is None else str(r.estimate),
            "ci_lo": None if r.lo is None else str(r.lo),
            "ci_hi": None if r.hi is None else str(r.hi),
            "n": r.n, "verdict": r.verdict,
            "payload_json": json.dumps({
                "name": r.name, "primary": r.primary, "unit": r.unit,
                "label": r.label, "credits": r.credits, "note": r.note,
                "z": round(r.z, 4), "detail": r.detail})})


def run(cfg, con, hist=None, only: list[str] | None = None) -> list[LaneResult]:
    """Score every registered lane. A scorer that raises is reported BLOCKED
    with the exception text rather than taking the whole bench down — one
    broken lane must not hide the ten that work."""
    from arb.bench_lanes import LANES
    out = []
    for lane, (name, fn) in LANES.items():
        if only and lane not in only:
            continue
        credits = int(cfg["bench"]["credits_per_month"].get(lane, 0))
        try:
            r = fn(cfg, con, hist)
        except Exception as e:                      # noqa: BLE001 — see docstring
            r = LaneResult(lane=lane, name=name, primary="-",
                           note=f"scorer failed: {type(e).__name__}: {e}")
        r.lane, r.name, r.credits = lane, name, credits
        out.append(r)
    return out
