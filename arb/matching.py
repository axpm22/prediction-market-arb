"""Event matching (SPEC §8): names/aliases, event identity with date±1 bridging,
doubleheader clustering, sanity gates, complement-map legality, line keys."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from arb.logutil import get_logger
from arb.models import Event, Quote

log = get_logger("arb.matching", "logs/unmatched.log")
NY = ZoneInfo("America/New_York")
ALIASES_PATH = Path("data/aliases.json")
EVENT_KEY_ALIASES_PATH = Path("data/event_key_aliases.json")

# WINNER markets with a genuine two-outcome space (complement map legal).
# Soccer NEVER: NO(A) = {B ∪ draw} (§7.1).
# Deliberately EXCLUDED when the sports list grew (2026-07-29):
#   americanfootball_nfl — a regular-season game can end tied, so NO(A) = {B ∪ tie}
#   cricket              — Test draws and ODI ties are live outcomes
#   icehockey_nhl        — a shootout always produces a winner, but whether Kalshi
#                          settles on the post-shootout result is UNVERIFIED (no
#                          open NHL market until October). Verify, then add.
STRICT_BINARY_WINNER_SPORTS = {"baseball_mlb", "basketball_nba", "tennis", "mma",
                               "basketball_wnba"}
LAST_TOKEN_FALLBACK_SPORTS = {"baseball_mlb", "basketball_nba"}


def load_aliases() -> dict:
    return json.loads(ALIASES_PATH.read_text())


def _norm_text(s: str) -> str:
    return " ".join("".join(c for c in s.lower() if c.isalnum() or c.isspace()).split())


def normalize_team(sport: str, name: str, aliases: dict) -> str | None:
    n = _norm_text(name)
    if not n:
        return None
    table = aliases.get(sport, {})
    if n in table:
        return table[n]
    if sport in ("tennis", "mma"):
        toks = n.split()
        return f"{toks[-1]} {toks[0][0]}" if len(toks) >= 2 else None
    if sport in LAST_TOKEN_FALLBACK_SPORTS:
        # fallback only onto a KNOWN canonical nickname — unmatched is never forced
        tok = n.split()[-1]
        if tok in set(table.values()):
            return tok
    return None  # alias entry required otherwise — never single-token fuzz (§8.1)


def line_key(line) -> str | None:
    """Line as canonical Decimal string — NEVER float in keys (§8.3)."""
    if line is None:
        return None
    d = Decimal(str(line)).normalize()
    return str(d)


def complement_map(quote: Quote, other_outcome: str) -> Quote:
    """NO -> YES(complement), legal ONLY for strict two-outcome binaries (§7.1):
    winner markets in no-draw sports, and half-point totals. Soccer never;
    integer-line totals never (push). Illegal map returns the quote unchanged."""
    if quote.side != "NO":
        return quote
    if quote.market_type == "WINNER":
        if quote.sport not in STRICT_BINARY_WINNER_SPORTS:
            return quote
    elif quote.market_type == "TOTAL":
        lk = line_key(quote.line)
        if lk is None or "." not in lk:
            return quote  # integer line: NO(Over 9) = under-or-push ≠ UNDER 9
    else:
        return quote
    return replace(quote, outcome_label=other_outcome, side="YES")


@dataclass
class MatchedGroup:
    event: Event
    quotes: list
    suspect: str | None = None
    gamepk: int | None = None


def _cluster_by_start(quotes: list[Quote], tol_secs: int) -> list[list[Quote]]:
    dated = sorted((q for q in quotes if q.start_utc), key=lambda q: q.start_utc)
    undated = [q for q in quotes if not q.start_utc]
    clusters: list[list[Quote]] = []
    for q in dated:
        if clusters and (q.start_utc - clusters[-1][-1].start_utc).total_seconds() <= tol_secs:
            clusters[-1].append(q)
        else:
            clusters.append([q])
    if undated:
        if len(clusters) == 1:
            clusters[0].extend(undated)  # unambiguous; quotes stay start-unknown
        elif not clusters:
            clusters = [undated]
        else:
            log.info("dropping %d start-unknown quotes in ambiguous multi-game group",
                     len(undated))
    return clusters


def _suspect_reason(cfg, cluster: list[Quote]) -> str | None:
    starts = [q.start_utc for q in cluster if q.start_utc]
    if starts and (max(starts) - min(starts)) > timedelta(
            minutes=int(cfg["max_start_drift_mins"])):
        return "start-drift"
    by_outcome: dict = {}
    for q in cluster:
        by_outcome.setdefault((q.market_type, line_key(q.line), q.outcome_label, q.side),
                              []).append(q.cost)
    for costs in by_outcome.values():
        if max(costs) - min(costs) > cfg["match_suspect_prob_diff"]:
            return "match-suspect-price"
    return None


def build_groups(quotes: list[Quote], cfg, aliases: dict,
                 mlb_schedule: list[dict] | None = None) -> list[MatchedGroup]:
    tol = int(cfg["execution"]["doubleheader_cluster_tol_secs"])
    by_pair: dict[tuple, list[Quote]] = {}
    for q in quotes:
        a = normalize_team(q.sport, q.away, aliases)
        b = normalize_team(q.sport, q.home, aliases)
        if not a or not b or a == b:
            log.info("unmatched %s %s: %r vs %r", q.venue, q.sport, q.away, q.home)
            continue
        # §7.1: outcome_label is the NORMALIZED team (or DRAW/OVER/UNDER)
        if q.market_type == "WINNER":
            q.outcome_label = normalize_team(q.sport, q.outcome_label, aliases) \
                or q.outcome_label
        by_pair.setdefault((q.sport, frozenset((a, b))), []).append(q)

    sched_by_pair = {}
    for g in mlb_schedule or []:
        ka = normalize_team("baseball_mlb", g["away"], aliases)
        kb = normalize_team("baseball_mlb", g["home"], aliases)
        if ka and kb:
            sched_by_pair.setdefault(frozenset((ka, kb)), []).append(g)

    groups: list[MatchedGroup] = []
    for (sport, pair), qs in by_pair.items():
        clusters = _cluster_by_start(qs, tol)
        keyed: list[tuple] = []
        for cluster in clusters:
            starts = [q.start_utc for q in cluster if q.start_utc]
            start, grade, gamepk, game_no = (min(starts) if starts else None), \
                ("nominal" if starts else "absent"), None, None
            if sport == "baseball_mlb" and start is not None:
                window = timedelta(hours=int(cfg["schedule_match_window_hours"]))
                cands = [g for g in sched_by_pair.get(pair, [])
                         if abs(g["start_utc"] - start) <= window]
                if cands:
                    g = min(cands, key=lambda g: abs(g["start_utc"] - start))
                    # verified start outranks all venue times (§11.3)
                    start, grade = g["start_utc"], "verified"
                    gamepk, game_no = g["gamePk"], g.get("gameNumber")
            keyed.append((cluster, start, grade, gamepk, game_no))

        # canonical event_key; date±1 bridging falls out of clustering: the whole
        # cluster keys from ONE canonical start, so a 23:50/00:10 pair gets one key
        date_counts: dict[str, int] = {}
        for cluster, start, grade, gamepk, game_no in keyed:
            names = sorted(pair)
            date = start.astimezone(NY).date().isoformat() if start else "undated"
            date_counts[date] = date_counts.get(date, 0) + 1
            key = f"{sport}:{date}:{names[0]}:{names[1]}"
            n_same_day = sum(1 for _, s, *_ in keyed
                             if s and s.astimezone(NY).date().isoformat() == date)
            if n_same_day > 1:  # doubleheader: gameNumber primary, ordinal backstop (§8.2)
                key += f":dh{game_no or date_counts[date]}"
            event = Event(event_key=key, category="sports", sport=sport,
                          home=names[1], away=names[0], start_utc=start,
                          start_grade=grade, gamepk=gamepk)
            for q in cluster:
                q.event_key = key
                if grade == "verified":
                    q.start_grade = "verified"
                    q.start_utc = start
            groups.append(MatchedGroup(event=event, quotes=cluster,
                                       suspect=_suspect_reason(cfg, cluster),
                                       gamepk=gamepk))
    return groups
