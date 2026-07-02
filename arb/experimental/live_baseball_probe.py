"""LIVE_BASEBALL_EV Phase 0 — feasibility probe (LIVE_BASEBALL_BUILD_PROMPT).

NO detection, NO bet: this records the freshness/depth reality that decides whether the lane is
even possible (spec §5). It measures three things append-only to `live_probe_samples`:
  1. MLB Stats publish lag  = our_fetch_time - feed metaData.timeStamp   (§5.2)
  2. live venue depth/suspension per game per venue (Kalshi/Polymarket/SX, free)  (§5.3)
  3. live Odds API book count, strictly within a per-day credit budget (§5.1 — the hard blocker)
The report reduces these to percentiles and a verdict on whether max_state_age_secs=5 and $50
depth are achievable. Clock discipline (§1.4): every age is our own wall clock; feed-embedded
times are used ONLY to estimate publish lag, never differenced against another feed.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from arb.matching import load_aliases, normalize_name
from arb.store import Store
from arb.venues.mlbstats import MlbStatsAdapter

ODDS_ABORT_BELOW_CREDITS = 50  # existing rule (CLAUDE.md #2)


@dataclass(frozen=True)
class GameState:
    game_pk: int
    status: str
    inning: int | None
    half: str | None
    outs: int | None
    bases: str
    home_score: int | None
    away_score: int | None
    balls: int | None
    strikes: int | None
    publish_time: datetime | None
    home: str
    away: str


def parse_live_feed(raw: dict[str, Any]) -> GameState:
    gd = raw.get("gameData", {})
    ld = raw.get("liveData", {})
    ls = ld.get("linescore", {})
    offense = ls.get("offense", {})
    count = (ld.get("plays", {}).get("currentPlay", {}) or {}).get("count", {})
    teams = gd.get("teams", {})
    bases = "".join(b for b, key in (("1", "first"), ("2", "second"), ("3", "third")) if offense.get(key))
    return GameState(
        game_pk=int((gd.get("game", {}) or {}).get("pk") or 0),
        status=str((gd.get("status", {}) or {}).get("abstractGameState", "")),
        inning=ls.get("currentInning"),
        half=(ls.get("inningHalf") or None),
        outs=ls.get("outs"),
        bases=bases or "-",
        home_score=(ls.get("teams", {}).get("home", {}) or {}).get("runs"),
        away_score=(ls.get("teams", {}).get("away", {}) or {}).get("runs"),
        balls=count.get("balls"),
        strikes=count.get("strikes"),
        publish_time=_parse_feed_timestamp((raw.get("metaData", {}) or {}).get("timeStamp")),
        home=str((teams.get("home", {}) or {}).get("name", "")),
        away=str((teams.get("away", {}) or {}).get("name", "")),
    )


def _parse_feed_timestamp(ts: str | None) -> datetime | None:
    """MLB Stats metaData.timeStamp is 'YYYYMMDD_HHMMSS' in UTC."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def publish_lag_secs(state: GameState, fetched_at: datetime) -> float | None:
    """our_fetch_time - feed publish time (§5.2). None when the feed omitted a timestamp.
    Labeled a lag ESTIMATE: subject to clock skew, but the median offset IS the effective lag."""
    if state.publish_time is None:
        return None
    return (fetched_at - state.publish_time).total_seconds()


def is_terminal(status: str) -> bool:
    return status in ("Final", "Game Over", "Completed Early")


# --- credit math (printed FIRST, §5.1) ------------------------------------------------

def credit_math(cfg: dict) -> str:
    budget = int(cfg.get("max_odds_api_calls_per_day", 20))
    poll = int(cfg.get("probe_poll_secs", 10))
    per_game_hours = 3.0
    polls_per_game = int(per_game_hours * 3600 / poll)
    feed_calls_per_game = polls_per_game  # 1 credit each if we polled Odds API every poll
    return (
        "CREDIT MATH (§5.1 — the hard blocker):\n"
        f"  A real live-consensus feed would poll Odds API every {poll}s => ~{feed_calls_per_game} "
        f"credits/game over ~{per_game_hours:.0f}h — one game blows a month of the 500/mo cap.\n"
        f"  => Phase 0 samples live books at most {budget} calls/day (config "
        f"max_odds_api_calls_per_day), enough to measure 'how many live books exist' a few times, "
        f"nowhere near a consensus feed. Venue quotes + MLB Stats are free and polled per {poll}s.\n"
        f"  Conclusion: 5s freshness and a live Odds API consensus are contradictory on this budget."
    )


def _odds_calls_today(store: Store, now: datetime) -> int:
    with store.connect() as con:
        con.executescript("")  # no-op; table created by Store.init_db
        row = con.execute(
            "SELECT COUNT(*) FROM live_probe_samples WHERE sample_type='odds_book' "
            "AND substr(recorded_at,1,10)=?", (now.date().isoformat(),)).fetchone()
    return int(row[0])


# --- persistence ----------------------------------------------------------------------

def _insert(store: Store, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = ["recorded_at", "game_pk", "sample_type", "publish_lag_secs", "inning", "half", "outs",
            "game_status", "venue", "outcome_label", "market_type", "ask", "bid", "depth_usd",
            "suspended", "quote_age_secs", "n_live_books", "credits_remaining", "notes"]
    with store.connect() as con:
        con.executemany(
            f"INSERT INTO live_probe_samples({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [tuple(r.get(c) for c in cols) for r in rows])


def _game_pair(home: str, away: str, aliases) -> tuple[str, str]:
    return tuple(sorted([normalize_name(home, "baseball_mlb", aliases),
                         normalize_name(away, "baseball_mlb", aliases)]))


def _quote_pair(event_key: str) -> tuple[str, ...]:
    parts = event_key.split(":")
    return tuple(sorted(parts[2:4])) if len(parts) >= 4 else ()


# --- one poll -------------------------------------------------------------------------

def probe_once(store: Store, config, mlb: MlbStatsAdapter, venue_adapters: list,
               games: list[dict], sample_odds: bool) -> dict[str, int]:
    """One probe cycle over the target games. Returns per-type sample counts."""
    aliases = load_aliases()
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    counts = {"state": 0, "venue_quote": 0, "odds_book": 0, "ended": 0}

    # 1. MLB Stats publish lag + game state (per game, free)
    live_games = []
    for g in games:
        fetched_at = datetime.now(timezone.utc)
        state = parse_live_feed(mlb.live_feed(g["game_pk"]))
        if is_terminal(state.status):
            counts["ended"] += 1
            continue  # a game that ENDED must never produce a candidate (rebook-loop lesson)
        live_games.append((g, state))
        rows.append({"recorded_at": fetched_at.isoformat(), "game_pk": g["game_pk"],
                     "sample_type": "state", "publish_lag_secs": publish_lag_secs(state, fetched_at),
                     "inning": state.inning, "half": state.half, "outs": state.outs,
                     "game_status": state.status})
        counts["state"] += 1

    # 2. venue depth/suspension per game per venue (free)
    target_pairs = {_game_pair(g["home"], g["away"], aliases): g["game_pk"] for g, _ in live_games}
    for adapter in venue_adapters:
        try:
            quotes = adapter.fetch_quotes("baseball_mlb")
        except Exception as exc:  # noqa: BLE001 - one venue failing never kills the probe
            rows.append({"recorded_at": now.isoformat(), "game_pk": 0, "sample_type": "venue_quote",
                         "venue": adapter.name, "notes": f"fetch-failed: {exc}"})
            continue
        for q in quotes:
            gp = target_pairs.get(_quote_pair(q.event_key))
            if gp is None:
                continue
            age = (datetime.now(timezone.utc) - q.fetched_at).total_seconds()
            rows.append({
                "recorded_at": datetime.now(timezone.utc).isoformat(), "game_pk": gp,
                "sample_type": "venue_quote", "venue": q.venue, "outcome_label": q.outcome_label,
                "market_type": q.market_type, "ask": str(q.cost),
                "bid": None if q.best_bid is None else str(q.best_bid),
                "depth_usd": None if q.depth_usd is None else str(q.depth_usd),
                "suspended": int(bool(q.live)), "quote_age_secs": age})
            counts["venue_quote"] += 1

    # 3. live Odds API book count (budgeted — the credit blocker)
    if sample_odds and live_games:
        counts["odds_book"] += _sample_odds_books(store, config, live_games, aliases, rows, now)

    _insert(store, rows)
    return counts


def _sample_odds_books(store, config, live_games, aliases, rows, now) -> int:
    budget = int(getattr(config, "live_baseball_ev", {}).get("max_odds_api_calls_per_day", 20))
    if _odds_calls_today(store, now) >= budget:
        print(f"probe: Odds API daily budget ({budget}) reached — skipping live-book sample.")
        return 0
    from arb.venues.oddsapi import OddsApiAdapter, OddsApiCreditsLow
    try:
        adapter = OddsApiAdapter(config)
        quotes = adapter.fetch_quotes("baseball_mlb")
    except OddsApiCreditsLow as exc:
        print(f"probe: {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"probe: odds sample failed: {exc}")
        return 0
    if adapter.credits_remaining is not None and adapter.credits_remaining < ODDS_ABORT_BELOW_CREDITS:
        print(f"probe: Odds API credits {adapter.credits_remaining} < {ODDS_ABORT_BELOW_CREDITS} — abort odds sampling.")
        return 0
    books_by_game: dict[int, set[str]] = {}
    for q in quotes:
        gp = {_game_pair(g["home"], g["away"], aliases): g["game_pk"] for g, _ in live_games}.get(_quote_pair(q.event_key))
        if gp is not None:
            books_by_game.setdefault(gp, set()).add(q.book)
    for g, _state in live_games:
        n = len(books_by_game.get(g["game_pk"], set()))
        rows.append({"recorded_at": now.isoformat(), "game_pk": g["game_pk"], "sample_type": "odds_book",
                     "n_live_books": n, "credits_remaining": adapter.credits_remaining})
    return len(live_games)


# --- runner ---------------------------------------------------------------------------

def run_probe(config, store: Store, n_games: int, minutes: float,
              fixture_dir: Path | None = None) -> None:
    cfg = dict(getattr(config, "live_baseball_ev", {}) or {})
    print(credit_math(cfg))
    mlb = MlbStatsAdapter(fixture_dir)
    today = datetime.now(timezone.utc).date().isoformat()
    games = mlb.live_game_pks(today)[:max(1, n_games)]
    if not games:
        print(f"probe: no live MLB games right now ({today}); try during game hours.")
        return
    print(f"probe: {len(games)} live game(s): " + ", ".join(f"{g['away']}@{g['home']}#{g['game_pk']}" for g in games))
    from arb.venues.kalshi import KalshiAdapter
    from arb.venues.polymarket import PolymarketAdapter
    from arb.venues.sx import SxAdapter
    venue_adapters = [KalshiAdapter(config), PolymarketAdapter(config), SxAdapter(config)]

    poll = int(cfg.get("probe_poll_secs", 10))
    deadline = time.monotonic() + minutes * 60
    odds_every = max(1, int(60 / poll))  # sample odds ~once/min, budget permitting
    i = 0
    while time.monotonic() < deadline:
        i += 1
        counts = probe_once(store, config, mlb, venue_adapters, games, sample_odds=(i % odds_every == 1))
        print(f"probe poll {i}: {counts}")
        if all(v == 0 for k, v in counts.items() if k != "ended"):
            print("probe: all target games ended — stopping.")
            break
        time.sleep(poll)
    print("probe: done. Run with --report for the verdict.")


# --- report + verdict -----------------------------------------------------------------

def _pcts(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    vs = sorted(values)
    def pc(p): return round(vs[min(len(vs) - 1, int(p / 100 * len(vs)))], 2)
    return {"n": len(vs), "p50": pc(50), "p90": pc(90), "p99": pc(99),
            "min": round(vs[0], 2), "max": round(vs[-1], 2)}


def probe_report(store: Store, config) -> str:
    cfg = dict(getattr(config, "live_baseball_ev", {}) or {})
    with store.connect() as con:
        lags = [float(r[0]) for r in con.execute(
            "SELECT publish_lag_secs FROM live_probe_samples WHERE sample_type='state' AND publish_lag_secs IS NOT NULL")]
        depths = [float(r[0]) for r in con.execute(
            "SELECT depth_usd FROM live_probe_samples WHERE sample_type='venue_quote' AND depth_usd IS NOT NULL")]
        ages = [float(r[0]) for r in con.execute(
            "SELECT quote_age_secs FROM live_probe_samples WHERE sample_type='venue_quote' AND quote_age_secs IS NOT NULL")]
        depth_by_venue = con.execute(
            "SELECT venue, COUNT(*), COALESCE(AVG(CAST(depth_usd AS REAL)),0) FROM live_probe_samples "
            "WHERE sample_type='venue_quote' GROUP BY venue").fetchall()
        books = [int(r[0]) for r in con.execute(
            "SELECT n_live_books FROM live_probe_samples WHERE sample_type='odds_book' AND n_live_books IS NOT NULL")]
        n_state = con.execute("SELECT COUNT(*) FROM live_probe_samples WHERE sample_type='state'").fetchone()[0]

    lag_p = _pcts(lags)
    depth_p = _pcts(depths)
    age_p = _pcts(ages)
    depth_over_50 = round(100 * sum(1 for d in depths if d >= 50) / len(depths), 1) if depths else 0.0
    lines = [
        "# LIVE_BASEBALL_EV Phase 0 report", "",
        credit_math(cfg), "",
        f"State samples: {n_state}",
        f"MLB Stats publish lag (s): {lag_p or 'no data'}",
        f"Venue live top-of-book depth ($): {depth_p or 'no data'}  |  % of quotes >= $50: {depth_over_50}%",
        f"Venue quote age at read (s): {age_p or 'no data'}",
        "Depth by venue (venue, n, avg$): " + (", ".join(f"{v[0]}={int(v[1])}/${v[2]:.0f}" for v in depth_by_venue) or "none"),
        f"Live Odds API book count per sample: {_pcts([float(b) for b in books]) or 'no samples (budget/none live)'}",
        "",
    ]

    max_state = float(cfg.get("max_state_age_secs", 5))
    min_depth = float(cfg.get("min_depth_usd", 50))
    verdict = _verdict(lag_p, depth_p, depth_over_50, books, max_state, min_depth)
    lines.append("## VERDICT (§5)")
    lines.extend(verdict)
    return "\n".join(lines)


def _verdict(lag_p, depth_p, depth_over_50, books, max_state, min_depth) -> list[str]:
    out = []
    if not lag_p:
        out.append("- INSUFFICIENT DATA: no publish-lag samples — run the probe on live games first.")
        return out
    lag50 = lag_p["p50"]
    state_ok = lag50 <= max_state
    out.append(
        f"- max_state_age_secs={max_state:.0f}: publish-lag p50={lag50}s p90={lag_p['p90']}s => "
        + ("ACHIEVABLE (median within gate); set mlb_publish_lag_secs≈p50." if state_ok
           else f"NOT ACHIEVABLE at {max_state:.0f}s — the 5s gate would reject most snapshots; "
                f"raise max_state_age_secs toward p90≈{lag_p['p90']}s and widen live_ev_min_edge."))
    if depth_p:
        out.append(
            f"- ${min_depth:.0f} depth: live depth p50=${depth_p['p50']} ({depth_over_50}% of quotes >= ${min_depth:.0f}) => "
            + ("$50 lane is real." if depth_over_50 >= 50
               else f"the '$50' lane is mostly a MICRO lane in disguise — main-lane depth is rare; lean on the micro band (${depth_p['p50']} typical)."))
    else:
        out.append(f"- ${min_depth:.0f} depth: no venue depth samples (venues may not list live MLB depth) — treat depth as unknown/micro until measured.")
    if books:
        bp = _pcts([float(b) for b in books])
        out.append(
            f"- live consensus (≥3 books): Odds API returned p50={bp['p50']} books/sample => "
            + ("≥3 live books sometimes exist, but the credit budget forbids a real feed — expect fair_value=None most polls." if bp["p50"] >= 3
               else "rarely ≥3 live books AND the budget forbids polling — live sportsbook consensus is effectively unavailable; the lane runs mostly as NO-CONSENSUS diagnostics."))
    else:
        out.append("- live consensus: no odds-book samples (budget/none live) — assume live consensus is usually unavailable.")
    return out
