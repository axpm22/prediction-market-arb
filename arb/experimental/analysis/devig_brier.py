"""De-vig method selector (offline, read-only) — resolves PREGAME_EV_ISSUES #1 for de-vig.

Issue #1 says the mitigations (incl. devig_method) are off by default and "flipping them should
be gated on validation (Brier/CLV), not done blind", and #1/EV-#1 say to "pick the method
empirically by which minimizes Brier/log-loss on settled outcomes via forecast/scoring.py".

This does exactly that: for each de-vig method it recomputes the WINNER consensus p̂ from the
persisted oddsapi books on settled events, scores p̂ against the actual result (Brier, log-loss,
ECE), and ranks the methods. Lower Brier/log-loss = better-calibrated consensus = the method to
flip `devig_method` to. Settlement uses the keyless MLB path, so this is MLB-only for now.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from arb.forecast.scoring import brier, calibration_bins, expected_calibration_error, log_loss
from arb.pricing import DEVIG_METHODS, consensus_probabilities
from arb.quote_settlement import _mlb_scores_resolver
from arb.matching import load_aliases
from arb.store import Store


@dataclass(frozen=True)
class MethodScore:
    method: str
    n: int                 # outcome-level observations scored
    mean_brier: float
    mean_logloss: float
    ece: float


def score_pairs(pairs: list[tuple[float, bool]], clip: float = 1e-6, bins: int = 10) -> tuple[int, float, float, float]:
    """(n, mean Brier, mean log-loss, ECE) over (p_hat, won) pairs."""
    if not pairs:
        return (0, 0.0, 0.0, 0.0)
    briers = [brier(p, 1 if w else 0) for p, w in pairs]
    lls = [log_loss(p, 1 if w else 0, clip) for p, w in pairs]
    rows = [{"p_you": p, "outcome": 1 if w else 0} for p, w in pairs]
    ece = expected_calibration_error(calibration_bins(rows, bins), len(rows))
    return (len(pairs), round(sum(briers) / len(briers), 6), round(sum(lls) / len(lls), 6), ece)


def method_pairs(store: Store, method: str, min_books: int, fixture_dir: Path | None = None) -> list[tuple[float, bool]]:
    """(p_hat, won) for every WINNER consensus outcome on a settled event, under `method`."""
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT event_key, venue, book, market_type, line, outcome, raw_price, cost, fee_cost, live "
            "FROM quotes WHERE venue='oddsapi' AND market_type='WINNER' AND live=0")]
    if not rows:
        return []
    scores_for = _mlb_scores_resolver(rows, load_aliases(), fixture_dir)
    by_event: dict[str, list] = defaultdict(list)
    for r in rows:
        by_event[r["event_key"]].append(_Q(r))
    pairs: list[tuple[float, bool]] = []
    for event_key, quotes in by_event.items():
        scores = scores_for(event_key)
        if scores is None:
            continue
        best = max(scores.values())
        winners = [team for team, s in scores.items() if s == best]
        if len(winners) != 1:
            continue  # draw/ambiguous — MLB has none, skip defensively
        consensus = consensus_probabilities(quotes, min_books, method=method)
        for outcome, p_hat in consensus.items():
            pairs.append((float(p_hat), outcome == winners[0]))
    return pairs


def score_methods(store: Store, min_books: int, methods=tuple(DEVIG_METHODS),
                  fixture_dir: Path | None = None) -> list[MethodScore]:
    out: list[MethodScore] = []
    for method in methods:
        n, b, ll, ece = score_pairs(method_pairs(store, method, min_books, fixture_dir))
        out.append(MethodScore(method, n, b, ll, ece))
    return out


def format_methods(scores: list[MethodScore]) -> str:
    lines = ["De-vig method selector (MLB settled WINNER consensus; lower Brier/log-loss = better):",
             f"  {'METHOD':>13} {'N':>6} {'BRIER':>9} {'LOGLOSS':>9} {'ECE':>8}"]
    ranked = sorted([s for s in scores if s.n], key=lambda s: s.mean_brier)
    for s in scores:
        lines.append(f"  {s.method:>13} {s.n:>6} {s.mean_brier:>9.5f} {s.mean_logloss:>9.5f} {s.ece:>8.5f}")
    if ranked:
        best = ranked[0]
        cur = next((s for s in scores if s.method == "proportional"), None)
        verdict = f"best by Brier: {best.method}"
        if cur and best.method != "proportional" and cur.n:
            verdict += f" (vs proportional {cur.mean_brier:.5f}); flipping devig_method warranted IF the gap is stable."
        elif best.method == "proportional":
            verdict += " — the shipped default already wins; no flip warranted."
        lines.append("  " + verdict)
    else:
        lines.append("  (no settled MLB events with an oddsapi consensus yet)")
    return "\n".join(lines)


class _Q:
    """Minimal quote shim for consensus_probabilities (reads venue/raw_price/book/market_type/
    line/outcome_label/event_key)."""
    __slots__ = ("venue", "raw_price", "book", "market_type", "line", "outcome_label", "event_key")

    def __init__(self, row: dict) -> None:
        self.venue = "oddsapi"
        self.raw_price = Decimal(str(row["raw_price"]))
        self.book = row["book"]
        self.market_type = row["market_type"]
        self.line = None if row["line"] in (None, "") else Decimal(str(row["line"]))
        self.outcome_label = row["outcome"]
        self.event_key = row["event_key"]
