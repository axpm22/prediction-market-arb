"""LIVE_BASEBALL_EV — in-game MLB EV candidate recorder (LIVE_BASEBALL_EV_SPEC.md).

MEASUREMENT-ONLY, paper, betting OFF. A live "edge" is guilty until proven not-stale, so the
whole point of this lane is the freshness/stability gates and the VANISHED-vs-null diagnostic,
not the bet. This module is the pure, testable core (gates + single edge ladder + recheck);
the live HTTP polling loop and the Phase-0 feasibility probe are thin runners on top.

The lane is isolated end to end (kind="LIVE_BASEBALL_EV"); it never contributes to any clean
pregame total. All thresholds live in config.yaml's `live_baseball_ev` block (CLAUDE.md #4).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from arb.pricing import q6
from arb.store import Store

LIVE_KIND = "LIVE_BASEBALL_EV"


@dataclass(frozen=True)
class LiveSnapshot:
    """One evaluation moment for one venue outcome, with every age measured against OUR fetch
    clock (never by differencing two feeds' embedded clocks — there is no shared clock)."""
    event_key: str
    venue: str
    book: str
    outcome_label: str
    market_type: str = "WINNER"
    line: Decimal | None = None
    # executable price ($1 unit): ask + venue fees (NO slippage buffer — that's in the ladder).
    executable_cost: Decimal = Decimal("0")
    exit_bid_net: Decimal | None = None
    depth_usd: Decimal | None = None
    # fair value from >=3 live books, de-vigged, with a CI (min/median/max across books).
    fair_value: Decimal | None = None
    fair_value_lo: Decimal | None = None
    fair_value_hi: Decimal | None = None
    n_books: int = 0
    # freshness (seconds, our fetch clock)
    venue_age_secs: float = 0.0
    state_age_secs: float = 0.0
    consensus_age_secs: float = 0.0
    # stability
    suspended: bool = False
    secs_since_reopen: float = 1e9
    secs_since_play: float = 1e9
    secs_since_runner_change: float = 1e9
    secs_since_score_change: float = 1e9
    at_bat_live: bool = False
    count_age_secs: float = 0.0
    quote_in_play_window: bool = False
    # context recorded with the candidate
    inning: int | None = None
    half: str | None = None
    outs: int | None = None
    bases: str | None = None
    score: dict[str, int] = field(default_factory=dict)
    pitch_clock_secs: float | None = None


@dataclass(frozen=True)
class LiveDecision:
    status: str                     # REJECTED_STALE | REJECTED_UNSTABLE | RESEARCH | FRESH_CANDIDATE | NONE
    reason: str
    raw_edge: Decimal
    adjusted_edge: Decimal
    effective_state_age: float
    promoted: bool
    snapshot: LiveSnapshot


def _f(cfg: dict, key: str, default: str) -> Decimal:
    return Decimal(str(cfg.get(key, default)))


def freshness_rejection(s: LiveSnapshot, cfg: dict) -> str | None:
    """§1.4 — reject on any stale feed, a recent suspend/reopen, or a quote fetched inside a
    play-update window."""
    if s.venue_age_secs > float(cfg.get("max_venue_age_secs", 5)):
        return "stale-venue-quote"
    if s.state_age_secs > float(cfg.get("max_state_age_secs", 5)):
        return "stale-game-state"
    if s.consensus_age_secs > float(cfg.get("max_consensus_age_secs", 5)):
        return "stale-consensus"
    if s.suspended or s.secs_since_reopen < float(cfg.get("suspend_cooldown_secs", 10)):
        return "suspended-or-cooldown"
    if s.quote_in_play_window:
        return "quote-in-play-window"
    return None


def stability_rejection(s: LiveSnapshot, cfg: dict) -> str | None:
    """§1.5 — evaluate only in genuinely stable moments (baseball has no game clock; the
    pitch clock is the within-at-bat timer)."""
    if s.at_bat_live and s.count_age_secs > (s.pitch_clock_secs or 20.0):
        return "live-at-bat-stale-count"
    if s.secs_since_play < float(cfg.get("post_event_freeze_secs", 10)):
        return "post-event-freeze"
    if s.secs_since_runner_change < 10.0:
        return "runner-change-cooldown"
    if s.secs_since_score_change < 15.0:
        return "score-change-cooldown"
    return None


def edge_ladder(s: LiveSnapshot, cfg: dict) -> dict[str, Decimal | float]:
    """§1.8/§1.9 — one coherent ladder. raw_edge = fair_value - executable_cost; adjusted_edge
    subtracts the (Phase-1 flat) latency buffer, the fee/slippage buffer, and a model
    uncertainty buffer that scales with the fair-value CI width."""
    fair = s.fair_value if s.fair_value is not None else Decimal("0")
    raw_edge = q6(fair - s.executable_cost)
    latency = _f(cfg, "latency_buffer_init", "0.03")
    fee_buffer = _f(cfg, "fee_buffer", "0.005")
    ci_width = Decimal("0")
    if s.fair_value_lo is not None and s.fair_value_hi is not None:
        ci_width = max(Decimal("0"), s.fair_value_hi - s.fair_value_lo)
    model_unc = q6(ci_width * _f(cfg, "ci_buffer_mult", "0.5"))
    adjusted_edge = q6(raw_edge - latency - fee_buffer - model_unc)
    effective_state_age = float(s.state_age_secs) + float(cfg.get("mlb_publish_lag_secs", 0))
    return {"raw_edge": raw_edge, "adjusted_edge": adjusted_edge,
            "model_uncertainty_buffer": model_unc, "effective_state_age": effective_state_age}


def evaluate_live_candidate(s: LiveSnapshot, cfg: dict) -> LiveDecision:
    """Run the §1 ladder to the 'fresh candidate' rung (recheck is a separate step)."""
    ladder = edge_ladder(s, cfg)
    raw_edge, adjusted_edge = ladder["raw_edge"], ladder["adjusted_edge"]
    eff_age = float(ladder["effective_state_age"])
    micro = s.depth_usd is not None and s.depth_usd < _f(cfg, "min_depth_usd", "50")
    min_edge = _f(cfg, "micro_live_ev_min_edge", "0.10") if micro else _f(cfg, "live_ev_min_edge", "0.07")
    promoted = adjusted_edge >= min_edge

    def decide(status: str, reason: str) -> LiveDecision:
        return LiveDecision(status, reason, raw_edge, adjusted_edge, eff_age,
                            promoted and status == "FRESH_CANDIDATE", s)

    stale = freshness_rejection(s, cfg)
    if stale:
        return decide("REJECTED_STALE", stale)
    unstable = stability_rejection(s, cfg)
    if unstable:
        return decide("REJECTED_UNSTABLE", unstable)
    if s.n_books < int(cfg.get("min_live_books", 3)):
        return decide("RESEARCH", "under-min-books")  # 1-2 books: logged, never EV (§1.7)
    if raw_edge <= 0:
        return decide("NONE", "no-raw-edge")
    return decide("FRESH_CANDIDATE", "promoted" if promoted else "below-adjusted-bar")


def recheck(entry: LiveDecision, refetched: LiveSnapshot, cfg: dict) -> str:
    """§1.10/§1.11 — re-fetched state must independently pass freshness, the game state must be
    unchanged, and the edge must still be present. Otherwise LIVE_VANISHED (never missed EV).
    Consensus moving TO the venue is recorded but NOT auto-rejected (that's the hypothesis)."""
    if freshness_rejection(refetched, cfg) is not None:
        return "LIVE_VANISHED"
    e, r = entry.snapshot, refetched
    if (e.inning, e.half, e.outs, e.bases, tuple(sorted(e.score.items()))) != \
       (r.inning, r.half, r.outs, r.bases, tuple(sorted(r.score.items()))):
        return "LIVE_VANISHED"  # game state changed under us
    if edge_ladder(refetched, cfg)["raw_edge"] <= 0:
        return "LIVE_VANISHED"
    return "CONFIRMED"


def record_live_candidate(store: Store, poll_id: int, decision: LiveDecision,
                          recheck_result: str | None = None) -> int:
    """Append a LIVE_BASEBALL_EV row carrying full entry context (§1.12). Measurement-only:
    confirmed candidates are OPEN (settle by score later); everything else is quarantined via
    quality_flag so it never touches a clean total. Append-only; flat-$1 paper stake."""
    s = decision.snapshot
    # A confirmed, promoted candidate is the only "live" paper bet; all else is research.
    confirmed = recheck_result == "CONFIRMED"
    status = "OPEN" if (confirmed and decision.promoted) else "CLOSED"
    quality_flag = None if (confirmed and decision.promoted) else f"live-{decision.status.lower()}"
    if recheck_result == "LIVE_VANISHED":
        quality_flag = "live-vanished"
    leg = {
        "venue": s.venue, "book": s.book, "outcome_label": s.outcome_label,
        "market_type": s.market_type, "line": None if s.line is None else str(s.line),
        "cost": str(s.executable_cost), "event_key": s.event_key,
        "fair_value": None if s.fair_value is None else str(s.fair_value),
    }
    note = (
        f"live-baseball-ev: {decision.status} {decision.reason}"
        + (f" recheck={recheck_result}" if recheck_result else "")
        + f" | raw_edge={decision.raw_edge} adjusted_edge={decision.adjusted_edge}"
        f" eff_state_age={decision.effective_state_age:.1f}s venue_age={s.venue_age_secs:.1f}s"
        f" n_books={s.n_books} inning={s.inning}{s.half or ''} outs={s.outs} bases={s.bases}"
        f" score={s.score} promoted={decision.promoted}"
    )
    with store.connect() as con:
        cur = con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, edge_points, stake_usd, legs_json, optimistic_cost, status, placed_at,
                   notes, quality_flag)
               VALUES (?, ?, 'sports', 'baseball_mlb', ?, 0, 1, ?, ?, '1.00', ?, ?, ?, ?, ?, ?)""",
            (
                _fingerprint(s), poll_id, LIVE_KIND, str(decision.adjusted_edge),
                str(decision.raw_edge), json.dumps([leg]), str(s.executable_cost),
                status, datetime.now(timezone.utc).isoformat(), note, quality_flag,
            ),
        )
        return int(cur.lastrowid)


def _fingerprint(s: LiveSnapshot) -> str:
    return f"live:{s.event_key}:{s.venue}:{s.book}:{s.outcome_label}:{s.inning}{s.half or ''}:{s.outs}"
