"""Detection (SPEC §8.4): locked covering constructs (ARB same-venue / XSPREAD
cross-venue — RULE 1 taxonomy), divergences, guards, rejected-flag audit trail.

Every rejection returns a diagnostic dict destined for left_on_table (V1
silently `continue`d; bug E2)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from itertools import groupby

from arb.matching import MatchedGroup, complement_map, line_key
from arb.models import Opportunity, Quote, q6
from arb.pricing import divergence_spread

# WINNER outcome spaces that two YES legs can cover; soccer has a draw (§8.4)
NO_DRAW_SPORTS = {"baseball_mlb", "basketball_nba", "tennis", "mma"}


def _mid(q: Quote) -> Decimal | None:
    return (q.cost + q.bid) / 2 if q.bid is not None else None


def _reject(rejects, g, reason, quotes, kind="ARB", edge=None):
    rejects.append({
        "event_key": g.event.event_key, "reason": reason, "kind": kind,
        "venue_pair": ":".join(sorted({q.venue for q in quotes})) or "?",
        "edge_points": edge,
        "legs": [(q.venue, q.market_type, q.line, q.outcome_label, q.side, str(q.cost))
                 for q in quotes]})


def _zombie(cfg, q: Quote) -> bool:
    return (q.volume_24h is not None and q.volume_24h == 0 and q.bid is not None
            and (q.cost - q.bid) >= cfg["zombie_spread_min"])


def _locked_opp(cfg, g, legs, now, mode="pregame") -> tuple[Opportunity | None, str | None]:
    """Build an ARB/XSPREAD opportunity from covering legs, or a rejection reason."""
    s = sum(q.effective_cost for q in legs)
    if s >= 1:
        return None, None  # no edge; not a rejection, just no flag
    venues = {q.venue for q in legs}
    kind = "ARB" if len(venues) == 1 else "XSPREAD"  # RULE 1: never call cross-venue arb
    edge = q6(1 - s)
    roi = q6(1 / s - 1)
    skew = max(q.fetched_at for q in legs) - min(q.fetched_at for q in legs)
    skew_secs = skew.total_seconds()
    if skew_secs > float(cfg["max_leg_skew_secs"]):
        return None, "skew-outer"  # beyond the outer recording bound: never pairs
    live = mode == "live"
    status, notes = "OK", []
    if live:
        status = "REVIEW"  # live floors at REVIEW — belt over suspenders (classify
        notes.append("live")  # would review it anyway; A3)
    if kind == "XSPREAD":
        status = "REVIEW"  # cross-venue REVIEW-only forever by default (rule 6)
        notes.append("cross-venue")
    if roi > cfg["arb_stale_warn"]:
        status = "REVIEW"  # every locked kind (§8.4 v3.3); a log line is not a guard
        notes.append(f"stale-warn roi={roi}")
    # live simultaneity is TIGHTER, not looser (A3): 3s vs the pregame 10s
    skew_bound = float(cfg["live_lane"]["max_actionable_skew_secs"] if live
                       else cfg["execution"]["max_actionable_skew_secs"])
    if skew_secs > skew_bound:
        status = "REVIEW"
        notes.append(f"skew={skew_secs:.1f}s")
    if any(q.depth_usd is None or q.depth_usd < cfg["min_executable_usd"] for q in legs):
        status = "REVIEW"
        notes.append("thin-depth")
    stake = Decimal(1)
    stakes = [q6(stake * q.effective_cost / s) for q in legs]
    return Opportunity(kind=kind, category="sports", event=g.event, legs=list(legs),
                       edge=edge, stakes=stakes, status=status,
                       notes="; ".join(notes), live=live), None


def detect(cfg, groups: list[MatchedGroup], now: datetime | None = None,
           mode: str = "pregame") -> tuple[list[Opportunity], list[dict]]:
    """mode='pregame' (default, unchanged) or 'live' (A3): the in-play guard
    inverts, the per-quote live rejection is skipped, everything else — zombie,
    empty-book, complement legality, covering-set, exact lines, stale-warn,
    depth — applies identically. The draw hole does not relax in-play."""
    now = now or datetime.now(timezone.utc)
    opps: list[Opportunity] = []
    rejects: list[dict] = []

    for g in groups:
        in_play = g.event.start_utc is not None and g.event.start_utc <= now
        if mode == "pregame" and in_play:
            # in-play/final guard at detection (§8.4): drop started events
            _reject(rejects, g, "in-play", g.quotes)
            continue
        if mode == "live" and not in_play:
            _reject(rejects, g, "pregame-in-live-cycle", g.quotes)  # diagnostic
            continue

        usable = []
        for q in g.quotes:
            if q.start_grade == "absent":
                _reject(rejects, g, "start-unknown", [q])
            elif _zombie(cfg, q):
                _reject(rejects, g, "zombie", [q])
            elif q.live and mode == "pregame":
                _reject(rejects, g, "in-play", [q])
            else:
                usable.append(q)

        # complement map before fingerprints (§7.1); other_outcome = the other team
        outcomes = sorted({q.outcome_label for q in usable if q.market_type == "WINNER"})
        mapped = []
        for q in usable:
            if q.side == "NO" and q.market_type == "WINNER" and len(outcomes) == 2:
                other = outcomes[1] if q.outcome_label == outcomes[0] else outcomes[0]
                mapped.append(complement_map(q, other))
            elif q.side == "NO" and q.market_type == "TOTAL":
                mapped.append(complement_map(
                    q, "UNDER" if q.outcome_label == "OVER" else "OVER"))
            else:
                mapped.append(q)

        if g.suspect:
            # suspect groups may log divergences (tagged), never locked flags (§8.2)
            _reject(rejects, g, g.suspect, g.quotes)

        by_market = groupby(
            sorted(mapped, key=lambda q: (q.market_type, line_key(q.line) or "")),
            key=lambda q: (q.market_type, line_key(q.line) or ""))
        for (mtype, line), qs_iter in by_market:
            qs = list(qs_iter)
            # best executable quote per (outcome, side)
            best: dict[tuple, Quote] = {}
            for q in qs:
                k = (q.outcome_label, q.side)
                if k not in best or q.effective_cost < best[k].effective_cost:
                    best[k] = q

            if not g.suspect:  # locked constructs never come from suspect data (§8.2)
                covers: list[list[Quote]] = []
                if mtype == "WINNER":
                    outs = sorted({o for o, s in best if s == "YES"})
                    if g.event.sport in NO_DRAW_SPORTS and len(outs) == 2:
                        covers.append([best[(outs[0], "YES")], best[(outs[1], "YES")]])
                    elif g.event.sport not in NO_DRAW_SPORTS and len(outs) >= 2:
                        # two YES legs never cover a draw — record the audit trail
                        pair = [best[(outs[0], "YES")], best[(outs[1], "YES")]]
                        if sum(q.effective_cost for q in pair) < 1:
                            _reject(rejects, g, "covering-set-fail", pair,
                                    edge=q6(1 - sum(q.effective_cost for q in pair)))
                else:  # TOTAL: OVER+UNDER at the EXACT same line (8.5 != 9.0)
                    if ("OVER", "YES") in best and ("UNDER", "YES") in best:
                        covers.append([best[("OVER", "YES")], best[("UNDER", "YES")]])

                # same-proposition YES+NO (any sport incl. soccer — the endorsed construct)
                for out in {o for o, _ in best}:
                    if (out, "YES") in best and (out, "NO") in best:
                        covers.append([best[(out, "YES")], best[(out, "NO")]])

                for legs in covers:
                    if len({(q.venue, q.side, q.outcome_label) for q in legs}) < len(legs):
                        continue
                    opp, why = _locked_opp(cfg, g, legs, now, mode)
                    if opp:
                        opps.append(opp)
                    elif why:
                        _reject(rejects, g, why, legs, kind="XSPREAD"
                                if len({q.venue for q in legs}) > 1 else "ARB")

            # divergence: same outcome+side across venues on raw mids (§7.2)
            for (out, side), _q in list(best.items()):
                same = [q for q in qs if q.outcome_label == out and q.side == side
                        and _mid(q) is not None]
                for i in range(len(same)):
                    for j in range(i + 1, len(same)):
                        a, b = same[i], same[j]
                        if a.venue == b.venue:
                            continue
                        spread = divergence_spread(_mid(a), _mid(b))
                        if abs(spread) >= cfg["divergence_threshold"]:
                            note = "match-suspect" if g.suspect else ""
                            opps.append(Opportunity(
                                kind="DIVERGENCE", category="sports", event=g.event,
                                legs=[a, b], edge=q6(abs(spread)), stakes=[],
                                status="REVIEW", notes=note))
    return opps, rejects
