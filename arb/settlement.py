from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .matching import event_key, load_aliases, normalize_name
from .pricing import q6
from .store import Store
from .venues.mlbstats import MlbStatsAdapter
from .venues.oddsapi import OddsApiAdapter

SETTLE_TRIPWIRE_TAG = "settle-tripwire"
# Inlined so core settlement never imports the experimental annotations module (loop_audit does
# the same). Identical to arb.experimental.annotations.SCHEMA.
_ANNOTATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bet_id) REFERENCES flat_bets(id)
);
"""


def _apply_settle_tripwire(con, new_id, placed_at_raw, settled_at, tripwire_secs) -> bool:
    """Instant-settle tripwire (FIX_SETTLED_REBOOK_LOOP #3): if a just-settled row's
    settled_at - placed_at is under one poll interval, its outcome was knowable at placement —
    never a forecast, regardless of win/loss. The settle still stands (append-only); this
    annotates `settle-tripwire` on the SAME transaction (so the row is never visible as
    settled-without-annotation), which excludes it from every P&L cell and blacklists its
    fingerprint from re-booking (tracker consults the tag). Insurance, not a filter — it fires
    whichever upstream guard let a stale book / backfilled score / clock skew through. Returns
    True if it fired; never blocks settlement."""
    if tripwire_secs is None:
        return False
    placed = _parse_utc(placed_at_raw)
    if placed is None:
        return False
    delta = (settled_at - placed).total_seconds()
    if delta >= float(tripwire_secs):
        return False
    # con.execute (NOT executescript, which implicitly COMMITs first) so the settled row and this
    # annotation land in the SAME transaction — the row is never visible as settled-without-tripwire.
    con.execute(_ANNOTATIONS_SCHEMA)
    con.execute(
        "INSERT INTO annotations(bet_id, tag, reason, created_at) VALUES (?, ?, ?, ?)",
        (int(new_id), SETTLE_TRIPWIRE_TAG,
         f"settled {delta:.1f}s after placement (< {tripwire_secs}s poll interval); outcome "
         f"knowable at placement — excluded from P&L and fingerprint blacklisted from re-booking",
         settled_at.isoformat()),
    )
    print(f"CRITICAL settle-tripwire: bet {new_id} settled {delta:.1f}s after placement "
          f"(< {tripwire_secs}s) — annotated, excluded, fingerprint blacklisted")
    return True


def settle_sports(store: Store, config, fixture_dir: Path | None = None) -> int:
    """Resolve open flat bets against final scores; append SETTLED rows, never edit.

    ARB bets: the locked-at-entry P&L is PROVISIONAL. When scores show the event
    completed, a SETTLED row realizes it at the same value — venue-resolution
    cross-checking (which is what can make realized differ) appends corrections later.
    """
    bets = [*_open_ev_bets(store), *_open_arb_bets(store)]
    if not bets:
        # No EV/ARB to settle, but parlays may still be open (own ledger lane, SPEC §11).
        return settle_parlays(store, config, fixture_dir)
    adapter = OddsApiAdapter(config, fixture_dir)
    results: dict[str, dict[str, Decimal]] = {}
    for sport in sorted({str(bet["sport"]) for bet in bets}):
        try:
            events = adapter.fetch_scores(sport, config.settle_days_from)
        except FileNotFoundError:
            continue
        results.update(score_results(events, sport))
    settled = 0
    now_utc = datetime.now(timezone.utc)
    with store.connect() as con:
        for bet in bets:
            leg = json.loads(bet["legs_json"])[0]
            # Never settle a game that hasn't started (leg_start in the future) — the matched
            # final is necessarily a different game (doubleheader/mislabeled slot). Grading it
            # WON here settled pregame EV bets and let the tracker reopen them into 30x dup rows.
            leg_start = _leg_start_utc(leg)
            if leg_start is not None and leg_start > now_utc:
                continue
            scores = results.get(str(leg["event_key"]))
            if scores is None:
                continue
            if bet["kind"] == "ARB":
                _append_arb_resolved_row(con, bet, config.settle_tripwire_secs)
                settled += 1
                continue
            result = _leg_result(leg, scores)
            if result is None:
                continue
            _append_settled_row(con, bet, result, "score-settled", config.settle_tripwire_secs)
            settled += 1
    # Parlays settle in their own lane on the same scores pass (SPEC §11).
    settled += settle_parlays(store, config, fixture_dir)
    return settled


def settle_parlays(store: Store, config, fixture_dir: Path | None = None) -> int:
    """Resolve open PARLAY bets as the AND of their component legs (SPEC §11).

    A parlay pays $1 iff EVERY leg wins. Each component leg is resolved against final scores
    exactly like an EV leg; the basket result is the conjunction:
      - all legs WON                -> WON   (P&L = (1−ask)/ask on the $1 stake)
      - any leg LOST                -> LOST  (−$1)
      - no loss but a leg VOID      -> VOID  ($0) — conservative until per-venue parlay void
                                       rules are verified (some venues drop the void leg).
      - any leg not yet resolved    -> stay OPEN (skip this pass).
    Append-only; idempotent (settled parlays are excluded by the open query).
    """
    bets = _open_parlay_bets(store)
    if not bets:
        return 0
    adapter = OddsApiAdapter(config, fixture_dir)
    results: dict[str, dict[str, Decimal]] = {}
    sports = {str(leg.get("sport") or _sport_from_event_key(leg)) for bet in bets
              for leg in _parlay_component_legs(bet)}
    for sport in sorted(s for s in sports if s):
        try:
            events = adapter.fetch_scores(sport, config.settle_days_from)
        except FileNotFoundError:
            continue
        results.update(score_results(events, sport))
    settled = 0
    with store.connect() as con:
        for bet in bets:
            components = _parlay_component_legs(bet)
            leg_results: list[str] = []
            for leg in components:
                scores = results.get(str(leg["event_key"]))
                if scores is None:
                    break
                res = _leg_result(leg, scores)
                if res is None:
                    break
                leg_results.append(res)
            if len(leg_results) != len(components):
                continue  # at least one leg unresolved — leave OPEN for a later pass
            result = _parlay_result(leg_results)
            _append_parlay_settled_row(con, bet, result, "parlay-score-settled", config.settle_tripwire_secs,
                                       sx_win_fee_rate=getattr(config, "parlay_sx_win_fee_rate", Decimal("0")))
            settled += 1
    return settled


def parlay_sx_win_fee(pnl: Decimal | None, legs: list, sx_win_fee_rate) -> Decimal | None:
    """SX charges its 5% fee ONLY on winning parlays (learn.sx.bet/trading/fees, read 2026-07-02) —
    singles are 0%. If a parlay won and any component leg is on SX, net the fee off the winnings
    (applied to the profit, never the stake). No-op for losses, voids, or non-SX parlays."""
    if pnl is None or pnl <= 0 or not sx_win_fee_rate:
        return pnl
    if any(str(leg.get("venue")) == "sx" for leg in legs):
        return q6(pnl * (Decimal("1") - Decimal(str(sx_win_fee_rate))))
    return pnl


def _parlay_result(leg_results: list[str]) -> str:
    if "LOST" in leg_results:
        return "LOST"
    if "VOID" in leg_results:
        return "VOID"
    return "WON"


def _parlay_component_legs(bet: sqlite3.Row) -> list[dict[str, Any]]:
    """Component single-leg quotes of a parlay bet: every leg except the synthetic PARLAY
    basket leg (market_type='PARLAY', outcome_label='PARLAY')."""
    legs = json.loads(bet["legs_json"] or "[]")
    return [leg for leg in legs if leg.get("market_type") != "PARLAY"]


def _parlay_basket_leg(bet: sqlite3.Row) -> dict[str, Any] | None:
    legs = json.loads(bet["legs_json"] or "[]")
    for leg in legs:
        if leg.get("market_type") == "PARLAY":
            return leg
    return None


def _sport_from_event_key(leg: dict[str, Any]) -> str:
    return str(leg.get("event_key") or "").split(":", 1)[0]


def _append_parlay_settled_row(con: sqlite3.Connection, bet: sqlite3.Row, result: str, note: str,
                               tripwire_secs: int | None = None,
                               sx_win_fee_rate: Decimal = Decimal("0")) -> None:
    # Pregame-only guard (mirrors EV/ARB): a parlay placed at/after the earliest leg start is a
    # post-hoc artifact, not a pregame bet — record INVALID_IN_PLAY with no P&L.
    placed_at = _parse_utc(bet["placed_at"])
    starts = [_leg_start_utc(leg) for leg in _parlay_component_legs(bet)]
    earliest = min((s for s in starts if s is not None), default=None)
    if placed_at is not None and earliest is not None and placed_at >= earliest:
        _append_invalid_row(con, bet, "INVALID_IN_PLAY", "invalid-in-play", f"{note}; invalid-in-play")
        return
    pnl = _sized_pnl(bet["optimistic_cost"], result, bet["stake_usd"])
    pnl = parlay_sx_win_fee(pnl, _parlay_component_legs(bet), sx_win_fee_rate)  # SX 5% on winning parlays
    settled_at = datetime.now(timezone.utc)
    cur = con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, status, placed_at, settled_at, result, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd, validation_rule, validation_tag)
        VALUES (?, ?, ?, ?, ?, 'PARLAY', ?, ?, ?, ?, ?, ?, NULL, ?, NULL, 'SETTLED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bet["fingerprint"], bet["id"], bet["opened_poll_id"], bet["category"], bet["sport"],
            bet["trusted"], bet["live"], bet["edge"], bet["stake_usd"], bet["legs_json"],
            bet["optimistic_cost"], None if pnl is None else str(pnl),
            bet["placed_at"], settled_at.isoformat(), result, note,
            bet["quality_flag"], bet["kelly_f_star"], bet["kelly_stake_usd"],
            bet["validation_rule"], bet["validation_tag"],
        ),
    )
    _apply_settle_tripwire(con, cur.lastrowid, bet["placed_at"], settled_at, tripwire_secs)


def _open_parlay_bets(store: Store) -> list[sqlite3.Row]:
    with store.connect() as con:
        return list(
            con.execute(
                """
                SELECT * FROM flat_bets b
                 WHERE b.kind = 'PARLAY' AND b.status = 'OPEN'
                   AND NOT EXISTS (SELECT 1 FROM flat_bets s
                                    WHERE s.status IN ('SETTLED','CLOSED','EXITED') AND s.reappearance_of = b.id)
                 ORDER BY b.id
                """
            )
        )


def settle_backfill_mlb(store: Store, fixture_dir: Path | None = None,
                        tripwire_secs: int | None = None) -> int:
    """Resolve open MLB EV/ARB bets against the MLB Stats API (no lookback window).

    Rescues the backlog the Odds API's 3-day window orphaned. Matches each open bet to a
    final game by stored start time when available, then falls back to the event_key's
    coarse UTC date + 6-hour bucket. Doubleheaders are settled only when the exact start
    time selects one game, or when every candidate final gives the same result for the
    stored leg. Append-only and idempotent: bets already SETTLED are skipped by the
    open-bet queries; re-running is a no-op.
    """
    bets = [b for b in (*_open_ev_bets(store), *_open_arb_bets(store))
            if str(b["sport"]) == "baseball_mlb"]
    if not bets:
        return 0
    aliases = load_aliases()
    adapter = MlbStatsAdapter(fixture_dir)

    # Which UTC dates do we need? Parse them from the event_keys, and fetch a +/-1 day
    # window around each so a late game logged under a different schedule date is caught.
    needed: set[str] = set()
    for bet in bets:
        leg = json.loads(bet["legs_json"])[0]
        parts = str(leg["event_key"]).split(":")
        if len(parts) >= 2:
            needed.add(parts[1])
        start = _leg_start_utc(leg)
        if start is not None:
            needed.add(start.date().isoformat())
    fetch_dates: set[str] = set()
    for d in needed:
        base = date.fromisoformat(d)
        for delta in (-1, 0, 1):
            fetch_dates.add((base + timedelta(days=delta)).isoformat())

    # Index final games by exact start time and by the legacy 6-hour bucket. New rows use
    # the exact time; old rows can still settle when every ambiguous final agrees.
    index: dict[tuple[str, frozenset, int], list[dict[str, Decimal]]] = {}
    date_pair_index: dict[tuple[str, frozenset], list[dict[str, Decimal]]] = {}
    time_pair_index: dict[tuple[str, frozenset], list[tuple[datetime, dict[str, Decimal]]]] = {}
    for fetch_date in sorted(fetch_dates):
        try:
            games = adapter.final_games(fetch_date)
        except FileNotFoundError:
            continue
        for game in games:
            nh = normalize_name(game["home"], "baseball_mlb", aliases)
            na = normalize_name(game["away"], "baseball_mlb", aliases)
            gd = game.get("game_date")
            if gd:
                utc_date = datetime.fromisoformat(str(gd).replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
            else:
                utc_date = fetch_date
            bucket = int(datetime.fromisoformat(str(gd).replace("Z", "+00:00")).astimezone(timezone.utc).hour // 6) if gd else -1
            scores = {nh: Decimal(str(game["home_score"])), na: Decimal(str(game["away_score"]))}
            pair = frozenset({nh, na})
            index.setdefault((utc_date, pair, bucket), []).append(scores)
            date_pair_index.setdefault((utc_date, pair), []).append(scores)
            if gd:
                start = datetime.fromisoformat(str(gd).replace("Z", "+00:00")).astimezone(timezone.utc)
                time_pair_index.setdefault((utc_date, pair), []).append((start, scores))

    settled = 0
    now_utc = datetime.now(timezone.utc)
    with store.connect() as con:
        for bet in bets:
            leg = json.loads(bet["legs_json"])[0]
            parts = str(leg["event_key"]).split(":")
            if len(parts) < 4:
                continue
            start_bucket = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else -1
            pair = frozenset({parts[2], parts[3]})
            leg_start = _leg_start_utc(leg)
            # Never settle a game that hasn't started. If leg_start is still in the future, the
            # game has no final yet — any (date, team-pair, ±2h) match is the wrong game (an
            # earlier doubleheader leg or a mislabeled slot). Grading it WON here is exactly what
            # settled pregame EV bets mid-afternoon and reopened them every watch poll into 30x
            # duplicate WON rows. A real settle fires once the game has actually finished.
            if leg_start is not None and leg_start > now_utc:
                continue
            bucket = _scores_for_start_time(time_pair_index, pair, leg_start)
            if bucket:
                pass
            elif start_bucket == -1:
                bucket = date_pair_index.get((parts[1], pair))
            else:
                bucket = index.get((parts[1], pair, start_bucket))
                if not bucket:
                    # Some venue keys land late games on the adjacent UTC date. Use the
                    # adjacent-date fallback; doubleheaders below settle only if the leg
                    # result is identical across every candidate final.
                    base = date.fromisoformat(parts[1])
                    bucket = [
                        scores
                        for delta in (-1, 1)
                        for scores in index.get(((base + timedelta(days=delta)).isoformat(), pair, start_bucket), [])
                    ]
            if not bucket:
                continue
            scores = bucket[0]
            if bet["kind"] == "ARB":
                if len(bucket) != 1:  # keep cross-venue event identity strict for arbs
                    continue
                _append_arb_resolved_row(con, bet, tripwire_secs)
                settled += 1
                continue
            if len(bucket) == 1:
                result = _leg_result(leg, scores)
                note = "mlbstats-settled"
            else:
                result = _consistent_result_across_candidates(leg, bucket)
                note = "mlbstats-settled; doubleheader-consistent-result"
            if result is None:
                continue
            _append_settled_row(con, bet, result, note, tripwire_secs)
            settled += 1
    return settled


def _open_arb_bets(store: Store) -> list[sqlite3.Row]:
    with store.connect() as con:
        return list(
            con.execute(
                """
                SELECT * FROM flat_bets b
                 WHERE b.kind = 'ARB' AND b.status = 'OPEN'
                   AND NOT EXISTS (SELECT 1 FROM flat_bets s
                                    WHERE s.status IN ('SETTLED','CLOSED','EXITED') AND s.reappearance_of = b.id)
                 ORDER BY b.id
                """
            )
        )


def _append_arb_resolved_row(con: sqlite3.Connection, bet: sqlite3.Row,
                             tripwire_secs: int | None = None) -> None:
    # Audit A3 — timing guard. A cross-venue lock is only real if both legs were quoted
    # before the event was live. If this row was placed at/after the event start, the
    # "arb" is a post-hoc time artifact (a quote captured after the result was effectively
    # known), not a lock. Record it INVALID_IN_PLAY and book no realized P&L — same gate
    # the EV path applies in _append_settled_row.
    leg = json.loads(bet["legs_json"])[0]
    placed_at = _parse_utc(bet["placed_at"])
    leg_start = _leg_start_utc(leg)
    if placed_at is not None and leg_start is not None and placed_at >= leg_start:
        _append_invalid_row(con, bet, "INVALID_IN_PLAY", "invalid-in-play",
                            "score-settled; invalid-in-play")
        return
    # Audit A1 — REVIEW lane. Un-whitelisted cross-venue arbs (trusted=0, hard rule #6)
    # still settle for observability, but into a flagged lane: quality_flag='REVIEW' keeps
    # them out of every clean/headline aggregation (all of which require quality_flag IS
    # NULL) and out of the sizing/capacity models (which require trusted=1). Only a
    # whitelisted venue-pair (config.trusted_pairs -> status OK -> trusted=1) settles clean.
    quality_flag = bet["quality_flag"] or (None if bet["trusted"] else "REVIEW")
    # Realized = provisional unless the venues resolved differently; scores alone cannot
    # see that, so the row is marked pending venue-resolution cross-check (corrections
    # append under the same bet id when that lands).
    settled_at = datetime.now(timezone.utc)
    cur = con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, pnl_confirmed,
                              pnl_kelly_optimistic, pnl_kelly_confirmed, status, placed_at, settled_at,
                              result, notes, quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag)
        VALUES (?, ?, ?, ?, ?, 'ARB', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'SETTLED', ?, ?, 'RESOLVED',
                ?, ?, ?, ?, ?, ?)
        """,
        (
            bet["fingerprint"], bet["id"], bet["opened_poll_id"], bet["category"], bet["sport"],
            bet["trusted"], bet["live"], bet["edge"], bet["stake_usd"], bet["legs_json"],
            bet["optimistic_cost"], bet["pnl_optimistic"], bet["pnl_confirmed"],
            bet["placed_at"], settled_at.isoformat(),
            "score-settled; realized=provisional; venue-resolution-cross-check-pending",
            quality_flag, bet["kelly_f_star"], bet["kelly_stake_usd"],
            bet["validation_rule"], bet["validation_tag"],
        ),
    )
    _apply_settle_tripwire(con, cur.lastrowid, bet["placed_at"], settled_at, tripwire_secs)


def settle_manual(store: Store, target_event_key: str, winning_outcome: str) -> int:
    """SPEC §9: non-sports (and anything scores can't resolve) settles by hand."""
    settled = 0
    with store.connect() as con:
        for bet in _open_ev_bets(store):
            leg = json.loads(bet["legs_json"])[0]
            if str(leg["event_key"]) != target_event_key:
                continue
            result = "WON" if str(leg["outcome_label"]) == winning_outcome else "LOST"
            _append_settled_row(con, bet, result, "manual-settled")
            settled += 1
    return settled


def score_results(events: list[dict[str, Any]], sport: str) -> dict[str, dict[str, Decimal]]:
    aliases = load_aliases()
    results: dict[str, dict[str, Decimal]] = {}
    for event in events:
        if not event.get("completed"):
            continue
        scores = {
            normalize_name(str(entry["name"]), sport, aliases): Decimal(str(entry["score"]))
            for entry in event.get("scores") or []
        }
        if len(scores) != 2:
            continue
        start = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
        key = event_key(sport, str(event["home_team"]), str(event["away_team"]), start, aliases)
        results[key] = scores
    return results


def _leg_start_utc(leg: dict[str, Any]) -> datetime | None:
    raw = leg.get("start_utc")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _scores_for_start_time(
    index: dict[tuple[str, frozenset], list[tuple[datetime, dict[str, Decimal]]]],
    pair: frozenset,
    start: datetime | None,
) -> list[dict[str, Decimal]] | None:
    if start is None:
        return None
    matches: list[dict[str, Decimal]] = []
    for delta in (-1, 0, 1):
        key = ((start.date() + timedelta(days=delta)).isoformat(), pair)
        # Align the bet start to the candidate date so the 2h window disambiguates the
        # doubleheader by time-of-day even when the venue keyed the game a UTC day off.
        shifted_start = start + timedelta(days=delta)
        for candidate_start, scores in index.get(key, []):
            if abs(candidate_start - shifted_start) <= timedelta(hours=2):
                matches.append(scores)
    return matches if len(matches) == 1 else None


def _leg_result(leg: dict[str, Any], scores: dict[str, Decimal]) -> str | None:
    label = str(leg["outcome_label"])
    if leg.get("market_type") == "TOTAL":
        if leg.get("line") is None:
            return None
        total = sum(scores.values(), Decimal("0"))
        line = Decimal(str(leg["line"]))
        if total == line:  # push on integer lines
            return "VOID"
        over_won = total > line
        return "WON" if (label == "OVER") is over_won else "LOST"
    best = max(scores.values())
    winners = [team for team, score in scores.items() if score == best]
    is_draw = len(winners) != 1
    # Soccer 1X2: a DRAW is a real, settleable outcome — not a permanent pending. The draw
    # leg wins on a tie and loses otherwise; a team leg loses on a tie.
    if label.lower() == "draw":
        return "WON" if is_draw else "LOST"
    if is_draw:
        return "LOST"
    return "WON" if winners[0] == label else "LOST"


def _consistent_result_across_candidates(leg: dict[str, Any], candidates: list[dict[str, Decimal]]) -> str | None:
    results = {_leg_result(leg, scores) for scores in candidates}
    if None in results or len(results) != 1:
        return None
    return next(iter(results))


def _open_ev_bets(store: Store) -> list[sqlite3.Row]:
    with store.connect() as con:
        return list(
            con.execute(
                """
                SELECT * FROM flat_bets b
                 WHERE b.kind IN ('EV', 'LIVE_BASEBALL_EV') AND b.status = 'OPEN'
                   AND NOT EXISTS (SELECT 1 FROM flat_bets s
                                    WHERE s.status IN ('SETTLED','CLOSED','EXITED') AND s.reappearance_of = b.id)
                 ORDER BY b.id
                """
            )
        )


def _append_settled_row(con: sqlite3.Connection, bet: sqlite3.Row, result: str, note: str,
                        tripwire_secs: int | None = None) -> None:
    leg = json.loads(bet["legs_json"])[0]
    placed_at = _parse_utc(bet["placed_at"])
    leg_start = _leg_start_utc(leg)
    if bet["kind"] != "LIVE_BASEBALL_EV" and placed_at is not None and leg_start is not None and placed_at >= leg_start:
        _append_invalid_row(con, bet, "INVALID_IN_PLAY", "invalid-in-play", f"{note}; invalid-in-play")
        return
    confirmed = con.execute(
        """SELECT confirmed_cost, validation_rule, validation_tag
             FROM flat_bets
            WHERE status = 'CONFIRMED' AND reappearance_of = ?
            ORDER BY id LIMIT 1""",
        (bet["id"],),
    ).fetchone()
    pnl_optimistic = _sized_pnl(bet["optimistic_cost"], result, bet["stake_usd"])
    pnl_confirmed = _sized_pnl(confirmed["confirmed_cost"], result, bet["stake_usd"]) if confirmed else None
    pnl_kelly_optimistic = _sized_pnl(bet["optimistic_cost"], result, bet["kelly_stake_usd"])
    pnl_kelly_confirmed = _sized_pnl(confirmed["confirmed_cost"], result, bet["kelly_stake_usd"]) if confirmed else None
    validation_rule = confirmed["validation_rule"] if confirmed and confirmed["validation_rule"] is not None else bet["validation_rule"]
    validation_tag = confirmed["validation_tag"] if confirmed and confirmed["validation_tag"] is not None else bet["validation_tag"]
    settled_at = datetime.now(timezone.utc)
    cur = con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, pnl_kelly_optimistic, pnl_kelly_confirmed,
                              status, placed_at, settled_at, result, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SETTLED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bet["fingerprint"],
            bet["id"],
            bet["opened_poll_id"],
            bet["category"],
            bet["sport"],
            bet["kind"],
            bet["trusted"],
            bet["live"],
            bet["edge"],
            bet["stake_usd"],
            bet["legs_json"],
            bet["optimistic_cost"],
            None if not confirmed else confirmed["confirmed_cost"],
            None if pnl_optimistic is None else str(pnl_optimistic),
            None if pnl_confirmed is None else str(pnl_confirmed),
            None if pnl_kelly_optimistic is None else str(pnl_kelly_optimistic),
            None if pnl_kelly_confirmed is None else str(pnl_kelly_confirmed),
            # P&L attributes to the day the bet was placed (SPEC §9); report restates that day.
            bet["placed_at"],
            settled_at.isoformat(),
            result,
            note,
            bet["quality_flag"],
            bet["kelly_f_star"],
            bet["kelly_stake_usd"],
            validation_rule,
            validation_tag,
        ),
    )
    # LIVE_BASEBALL_EV legitimately places and settles inside the game — exempt (as loop_audit does).
    if bet["kind"] != "LIVE_BASEBALL_EV":
        _apply_settle_tripwire(con, cur.lastrowid, bet["placed_at"], settled_at, tripwire_secs)


def _append_invalid_row(con: sqlite3.Connection, bet: sqlite3.Row, result: str, quality_flag: str, note: str) -> None:
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, pnl_kelly_optimistic, pnl_kelly_confirmed,
                              status, placed_at, settled_at, result, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL,
                'CLOSED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bet["fingerprint"],
            bet["id"],
            bet["opened_poll_id"],
            bet["category"],
            bet["sport"],
            bet["kind"],
            bet["live"],
            bet["edge"],
            bet["stake_usd"],
            bet["legs_json"],
            bet["optimistic_cost"],
            bet["placed_at"],
            datetime.now(timezone.utc).isoformat(),
            result,
            note,
            quality_flag,
            bet["kelly_f_star"],
            bet["kelly_stake_usd"],
            bet["validation_rule"],
            bet["validation_tag"],
        ),
    )


def _parse_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _flat_pnl(effective_cost: str | None, result: str) -> Decimal | None:
    """$1 flat stake at effective cost c: win pays (1-c)/c, loss pays -1, void pays 0."""
    return _sized_pnl(effective_cost, result, Decimal("1.00"))


def _sized_pnl(effective_cost: str | None, result: str, stake_usd: str | Decimal | None) -> Decimal | None:
    """Stake-sized P&L at effective cost c; $1 stake is the flat-bet special case.

    K10: stored at q6, NOT cents — money() booked every $1-stake bet with |P&L| < $0.005
    as exactly $0.00 (±0.5% of stake per row), erasing the 0-1% band. Cents are display."""
    if effective_cost is None or stake_usd in (None, ""):
        return None
    stake = Decimal(str(stake_usd))
    if stake <= 0:
        return Decimal("0.00")
    if result == "VOID":
        return Decimal("0.00")
    if result == "LOST":
        return q6(-stake)
    cost = Decimal(effective_cost)
    if cost <= 0:
        return None
    return q6(stake * (Decimal("1") - cost) / cost)
