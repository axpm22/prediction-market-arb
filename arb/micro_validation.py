"""M5 — validate the micro lane on its OWN realized edge before it is ever trusted.

`score_segments` and `grade_clv` both filter `quality_flag IS NULL`, so the thin-book micro
lane (`quality_flag='micro'`) is never graded — yet thin/illiquid books are exactly where stale
or mis-scaled prices breed phantom edge. This grades micro as its own segment: realized per-bet
ROI with a seeded bootstrap 95% CI, plus win rate. Micro stays quarantined (never headline)
until its ROI CI low is > 0 — i.e. the lane is profitable with statistical confidence, not just
on detection. Read-only over the append-only ledger; paper-only.
"""
from __future__ import annotations

from dataclasses import dataclass

from arb.experimental.ev.clv import _bootstrap_ci
from arb.store import Store


@dataclass(frozen=True)
class MicroLaneGrade:
    n: int                 # settled micro bets with a realized P&L
    decided: int           # WON+LOST (VOID/SOLD excluded from win rate)
    win_rate: float
    roi: float             # total realized P&L / total stake
    roi_ci_low: float      # seeded bootstrap 95% CI of the per-bet ROI mean
    roi_ci_high: float
    trustable: bool        # n >= min_n AND roi_ci_low > 0 → could be promoted off quarantine
    note: str


def grade_micro_lane(store: Store, min_n: int = 30, resamples: int = 10000) -> MicroLaneGrade:
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            """SELECT result, pnl_optimistic, stake_usd
                 FROM flat_bets
                WHERE kind='EV' AND quality_flag='micro' AND status IN ('SETTLED','EXITED')
                  AND result IN ('WON','LOST','VOID','SOLD')
                  AND pnl_optimistic IS NOT NULL AND CAST(stake_usd AS REAL) > 0
                ORDER BY id""")]
    rois = [float(r["pnl_optimistic"]) / float(r["stake_usd"]) for r in rows]
    n = len(rois)
    decided = sum(1 for r in rows if r["result"] in ("WON", "LOST"))
    wins = sum(1 for r in rows if r["result"] == "WON")
    win_rate = wins / decided if decided else 0.0
    total_pnl = sum(float(r["pnl_optimistic"]) for r in rows)
    total_stake = sum(float(r["stake_usd"]) for r in rows) or 1.0
    roi = total_pnl / total_stake
    lo, hi = _bootstrap_ci(rois, resamples) if n >= 2 else (0.0, 0.0)
    trustable = n >= min_n and lo > 0
    if n < min_n:
        note = f"INSUFFICIENT — {n}/{min_n} settled micro bets; keep quarantined."
    elif lo > 0:
        note = f"TRUSTABLE — ROI 95% CI [{lo:+.4f}, {hi:+.4f}] is above 0; micro is realizing edge."
    else:
        note = f"QUARANTINED — ROI 95% CI [{lo:+.4f}, {hi:+.4f}] includes 0; not proven yet."
    return MicroLaneGrade(n, decided, round(win_rate, 4), round(roi, 4), lo, hi, trustable, note)
