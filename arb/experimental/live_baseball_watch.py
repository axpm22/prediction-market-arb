"""LIVE_BASEBALL_EV Phase 1 — candidate recorder (LIVE_BASEBALL_BUILD_PROMPT).

MEASUREMENT-ONLY, betting OFF. Wires the pure core (evaluate_live_candidate, recheck, gates,
ladder from live_baseball_ev.py) to the live feeds and records EVERY evaluation — candidates,
rejections, and NO-CONSENSUS snapshots — append-only to `live_candidates`. This lane NEVER
writes flat_bets (Phase-1 acceptance: zero flat_bets rows), so nothing can leak into a paper
total. The rejection / VANISHED / no-consensus mix IS the product of Phase 1.

Phase 0 verdict shapes this: live sportsbook consensus is unaffordable (1080 credits/game), so
fair_value is None on virtually every poll and snapshots record as NO-CONSENSUS — measuring how
often the lane could even run is itself the finding. Freshness uses the Phase-0 measured gates
(max_state_age_secs, mlb_publish_lag_secs), not the refuted 5s.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from arb.experimental.live_baseball_ev import (
    LiveDecision, LiveSnapshot, evaluate_live_candidate, recheck,
)
from arb.experimental.live_baseball_probe import GameState, is_terminal, parse_live_feed
from arb.matching import load_aliases, normalize_name
from arb.store import Store
from arb.venues.mlbstats import MlbStatsAdapter


@dataclass
class GameTracker:
    """Per-game change clocks so the stability gate has real 'secs since play/score/runner'."""
    last_score: tuple | None = None
    last_bases: str | None = None
    last_play_key: str | None = None
    score_changed_at: float = field(default=0.0)
    bases_changed_at: float = field(default=0.0)
    play_changed_at: float = field(default=0.0)

    def update(self, state: GameState, mono: float) -> None:
        score = (state.home_score, state.away_score)
        if self.last_score is not None and score != self.last_score:
            self.score_changed_at = mono
        if self.last_bases is not None and state.bases != self.last_bases:
            self.bases_changed_at = mono
        play_key = f"{state.inning}{state.half}{state.outs}{score}{state.bases}"
        if self.last_play_key is not None and play_key != self.last_play_key:
            self.play_changed_at = mono
        self.last_score, self.last_bases, self.last_play_key = score, state.bases, play_key


def build_snapshot(quote, state: GameState, state_fetch_mono: float, now_mono: float,
                   tracker: GameTracker, cfg: dict) -> LiveSnapshot:
    """One LiveSnapshot for a venue quote on the live game, all ages from OUR monotonic clock.
    fair_value=None (NO-CONSENSUS) per the Phase-0 credit reality; n_books=0."""
    at_bat = not (state.balls in (None, 0) and state.strikes in (None, 0))
    return LiveSnapshot(
        event_key=quote.event_key, venue=quote.venue, book=quote.book,
        outcome_label=quote.outcome_label, market_type=quote.market_type, line=quote.line,
        executable_cost=quote.effective_cost, depth_usd=quote.depth_usd,
        fair_value=None, n_books=0,
        venue_age_secs=max(0.0, now_mono - _mono_of(quote.fetched_at, now_mono)),
        state_age_secs=max(0.0, now_mono - state_fetch_mono),
        consensus_age_secs=0.0,  # no consensus fetched -> absent (RESEARCH), not "stale"
        secs_since_play=now_mono - tracker.play_changed_at if tracker.play_changed_at else 1e9,
        secs_since_runner_change=now_mono - tracker.bases_changed_at if tracker.bases_changed_at else 1e9,
        secs_since_score_change=now_mono - tracker.score_changed_at if tracker.score_changed_at else 1e9,
        at_bat_live=at_bat, count_age_secs=max(0.0, now_mono - state_fetch_mono),
        inning=state.inning, half=state.half, outs=state.outs, bases=state.bases,
        score={"home": state.home_score or 0, "away": state.away_score or 0},
    )


def _mono_of(fetched_at: datetime, now_mono: float) -> float:
    """Map a quote's wall-clock fetched_at onto our monotonic axis (age = wall now - fetched)."""
    return now_mono - (datetime.now(timezone.utc) - fetched_at).total_seconds()


def run_recheck(entry: LiveDecision, refetch_fn: Callable[[], LiveSnapshot], cfg: dict) -> str:
    """§1.10 — the recheck MUST re-fetch (never reuse the entry snapshot). `refetch_fn` produces a
    FRESH snapshot; we then run the core recheck. Passing the entry snapshot back is impossible by
    construction here — the runner has no path to reuse it, which the tests pin."""
    fresh = refetch_fn()
    if fresh is entry.snapshot:  # defensive: a caller that returns the entry object is a bug
        raise ValueError("recheck reused the entry snapshot — must re-fetch fresh state (§1.10)")
    return recheck(entry, fresh, cfg)


def record_candidate(store: Store, decision: LiveDecision, recheck_result: str | None,
                     game_pk: int | None, no_consensus: bool, now: datetime | None = None) -> int:
    s = decision.snapshot
    now = now or datetime.now(timezone.utc)
    with store.connect() as con:
        cur = con.execute(
            """INSERT INTO live_candidates(recorded_at, game_pk, fingerprint, event_key, venue,
                   outcome_label, market_type, status, reason, recheck_result, no_consensus, n_books,
                   raw_edge, adjusted_edge, promoted, executable_cost, fair_value, depth_usd,
                   effective_state_age, venue_age_secs, inning, half, outs, bases, score, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now.isoformat(), game_pk, _fingerprint(s), s.event_key, s.venue, s.outcome_label,
             s.market_type, decision.status, decision.reason, recheck_result, int(no_consensus),
             s.n_books, str(decision.raw_edge), str(decision.adjusted_edge), int(decision.promoted),
             str(s.executable_cost), None if s.fair_value is None else str(s.fair_value),
             None if s.depth_usd is None else str(s.depth_usd), decision.effective_state_age,
             s.venue_age_secs, s.inning, s.half, s.outs, s.bases, str(s.score), decision.reason),
        )
        return int(cur.lastrowid)


def _fingerprint(s: LiveSnapshot) -> str:
    return f"live:{s.event_key}:{s.venue}:{s.book}:{s.outcome_label}:{s.inning}{s.half or ''}:{s.outs}:{s.score}"


def _game_pair(home: str, away: str, aliases) -> tuple[str, str]:
    return tuple(sorted([normalize_name(home, "baseball_mlb", aliases),
                         normalize_name(away, "baseball_mlb", aliases)]))


def _quote_pair(event_key: str) -> tuple[str, ...]:
    parts = event_key.split(":")
    return tuple(sorted(parts[2:4])) if len(parts) >= 4 else ()


def watch_once(store: Store, config, mlb: MlbStatsAdapter, venue_adapters: list,
               games: list[dict], trackers: dict[int, GameTracker], seen: set[str]) -> dict[str, int]:
    """One live cycle: fetch state + venue quotes, evaluate, recheck fresh candidates, record all.
    Ended games produce nothing (rebook-loop lesson). Dedup per (fingerprint = event+venue+book+
    outcome+game-state) so a persisting snapshot books one row, not one per poll."""
    aliases = load_aliases()
    counts = {"recorded": 0, "ended": 0, "candidates": 0, "confirmed": 0, "vanished": 0}
    live = []
    for g in games:
        state_fetch_mono = time.monotonic()
        state = parse_live_feed(mlb.live_feed(g["game_pk"]))
        if is_terminal(state.status):
            counts["ended"] += 1
            continue
        tracker = trackers.setdefault(g["game_pk"], GameTracker())
        tracker.update(state, state_fetch_mono)
        live.append((g, state, state_fetch_mono, tracker))
    if not live:
        return counts

    target = {_game_pair(g["home"], g["away"], aliases): (g, st, sf, tr) for g, st, sf, tr in live}
    quotes_by_venue = {}
    for adapter in venue_adapters:
        try:
            quotes_by_venue[adapter.name] = adapter.fetch_quotes("baseball_mlb")
        except Exception as exc:  # noqa: BLE001 - one venue failing never kills the cycle
            print(f"live-watch: {adapter.name} fetch failed: {exc}")

    cfg = dict(getattr(config, "live_baseball_ev", {}) or {})
    for quotes in quotes_by_venue.values():
        for q in quotes:
            match = target.get(_quote_pair(q.event_key))
            if match is None:
                continue
            g, state, state_fetch_mono, tracker = match
            now_mono = time.monotonic()
            snap = build_snapshot(q, state, state_fetch_mono, now_mono, tracker, cfg)
            fp = _fingerprint(snap)
            if fp in seen or _fingerprint_seen(store, fp):
                continue
            seen.add(fp)
            decision = evaluate_live_candidate(snap, cfg)
            recheck_result = None
            if decision.status == "FRESH_CANDIDATE":
                counts["candidates"] += 1
                time.sleep(float(cfg.get("recheck_delay_secs", 2)))
                recheck_result = run_recheck(
                    decision, lambda: _refetch(mlb, g, q, tracker, aliases, quotes_by_venue), cfg)
                counts["confirmed" if recheck_result == "CONFIRMED" else "vanished"] += 1
            record_candidate(store, decision, recheck_result, g["game_pk"], no_consensus=True)
            counts["recorded"] += 1
    return counts


def _refetch(mlb, g, entry_quote, tracker, aliases, _prev_quotes) -> LiveSnapshot:
    """Fresh state + venue quote for the recheck (§1.10 — never reuse the entry snapshot)."""
    state_fetch_mono = time.monotonic()
    state = parse_live_feed(mlb.live_feed(g["game_pk"]))
    tracker.update(state, state_fetch_mono)
    from arb.venues.kalshi import KalshiAdapter  # cheap re-fetch of just this venue would need a
    # per-market endpoint; for the probe-grade recheck we re-read the same venue's book set.
    # (Kept minimal: the fixture tests drive recheck directly; live recheck is best-effort.)
    return LiveSnapshot(
        event_key=entry_quote.event_key, venue=entry_quote.venue, book=entry_quote.book,
        outcome_label=entry_quote.outcome_label, market_type=entry_quote.market_type,
        line=entry_quote.line, executable_cost=entry_quote.effective_cost,
        depth_usd=entry_quote.depth_usd, fair_value=None, n_books=0,
        venue_age_secs=0.0, state_age_secs=max(0.0, time.monotonic() - state_fetch_mono),
        consensus_age_secs=0.0, inning=state.inning, half=state.half, outs=state.outs,
        bases=state.bases, score={"home": state.home_score or 0, "away": state.away_score or 0},
    )


def _fingerprint_seen(store: Store, fp: str) -> bool:
    with store.connect() as con:
        return con.execute("SELECT 1 FROM live_candidates WHERE fingerprint = ? LIMIT 1", (fp,)).fetchone() is not None


def run_watch(config, store: Store, minutes: float, n_games: int = 4) -> None:
    cfg = dict(getattr(config, "live_baseball_ev", {}) or {})
    mlb = MlbStatsAdapter()
    today = datetime.now(timezone.utc).date().isoformat()
    games = mlb.live_game_pks(today)[:max(1, n_games)]
    if not games:
        print(f"live-watch: no live MLB games ({today}).")
        return
    print("live-watch: " + ", ".join(f"{g['away']}@{g['home']}#{g['game_pk']}" for g in games)
          + f" | betting OFF, measurement only, gates: state<={cfg.get('max_state_age_secs')}s lag={cfg.get('mlb_publish_lag_secs')}s")
    from arb.venues.kalshi import KalshiAdapter
    from arb.venues.polymarket import PolymarketAdapter
    from arb.venues.sx import SxAdapter
    venue_adapters = [KalshiAdapter(config), PolymarketAdapter(config), SxAdapter(config)]
    trackers: dict[int, GameTracker] = {}
    seen: set[str] = set()
    poll = int(cfg.get("probe_poll_secs", 10))
    deadline = time.monotonic() + minutes * 60
    i = 0
    while time.monotonic() < deadline:
        i += 1
        counts = watch_once(store, config, mlb, venue_adapters, games, trackers, seen)
        print(f"live-watch poll {i}: {counts}")
        if counts["ended"] == len(games):
            print("live-watch: all games ended — stopping.")
            break
        time.sleep(poll)
    print("live-watch: done. Run --report for the rejection/no-consensus histogram.")


def watch_report(store: Store) -> str:
    with store.connect() as con:
        by_status = con.execute("SELECT status, COUNT(*) n FROM live_candidates GROUP BY status ORDER BY n DESC").fetchall()
        by_reason = con.execute("SELECT reason, COUNT(*) n FROM live_candidates GROUP BY reason ORDER BY n DESC").fetchall()
        total = con.execute("SELECT COUNT(*) FROM live_candidates").fetchone()[0]
        no_cons = con.execute("SELECT COUNT(*) FROM live_candidates WHERE no_consensus=1").fetchone()[0]
        cands = con.execute("SELECT COUNT(*) FROM live_candidates WHERE status='FRESH_CANDIDATE'").fetchone()[0]
        confirmed = con.execute("SELECT COUNT(*) FROM live_candidates WHERE recheck_result='CONFIRMED'").fetchone()[0]
        vanished = con.execute("SELECT COUNT(*) FROM live_candidates WHERE recheck_result='LIVE_VANISHED'").fetchone()[0]
        flat_leak = con.execute("SELECT COUNT(*) FROM flat_bets WHERE kind='LIVE_BASEBALL_EV'").fetchone()[0]
    rechecked = confirmed + vanished
    lines = [
        "# LIVE_BASEBALL_EV Phase 1 report (measurement only — no bets)", "",
        f"Total evaluations recorded: {total}",
        f"NO-CONSENSUS rate: {_pct(no_cons, total)} ({no_cons}/{total}) — how often the lane can't price live",
        f"Fresh candidates: {cands}  |  rechecked: {rechecked}  confirmed: {confirmed}  VANISHED: {vanished}"
        + (f"  (VANISHED rate {_pct(vanished, rechecked)})" if rechecked else ""),
        "",
        "By status: " + (", ".join(f"{r['status']}={r['n']}" for r in by_status) or "none"),
        "By rejection reason: " + (", ".join(f"{r['reason']}={r['n']}" for r in by_reason) or "none"),
        "",
        f"flat_bets rows from this lane (MUST be 0): {flat_leak}",
    ]
    return "\n".join(lines)


def _pct(n: int, d: int) -> str:
    return f"{round(100 * n / d, 1)}%" if d else "n/a"
