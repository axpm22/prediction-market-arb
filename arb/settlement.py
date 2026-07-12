"""Settlement (SPEC §11): resolution-settled kinds resolve via oracles; every
resolution appends ONE terminal `settled` bet_events row — rule 9's mechanism.
Tripwire, timing guards, VOID/PARTIAL conventions live here."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from arb.logutil import get_logger
from arb.matching import line_key

log = get_logger("arb.settle", "logs/scan.log")
NY = ZoneInfo("America/New_York")

# Registered settlement paths (§12): a kind without a settler is a CONFIG ERROR
# at startup, not a permanent-OPEN surprise later (bug E3). XSPREAD's registered
# settler IS the locked-at-entry path (L1).
SETTLEMENT_PATHS = {"ARB": "entry", "XSPREAD": "entry", "POLITICAL": "entry",
                    "EV": "resolution", "NEGRISK": "resolution"}


def validate_settlement_paths(cfg) -> None:
    from arb.config import ConfigError
    if cfg["btc_arb"]["enabled"] and "BTC" not in SETTLEMENT_PATHS:
        raise ConfigError(
            "btc_arb.enabled but BTC has no registered settlement path — build the "
            "same-oracle settle path first (§12; V1 gap E3: permanent-OPEN bets)")


def open_resolution_bets(store):
    return store.query(
        """SELECT fb.* FROM flat_bets fb
           WHERE fb.kind IN ('EV', 'NEGRISK', 'BTC')
             AND NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id = fb.id
                             AND be.kind IN ('settled', 'void'))""")


def _leg_result(leg: dict, winner: str, total: Decimal | None = None) -> str:
    """WON/LOST/VOID for one leg given the winning outcome token (or OVER/UNDER
    semantics from the final total)."""
    if winner == "VOID":
        return "VOID"
    if leg.get("market_type") == "TOTAL" and total is not None:
        line = Decimal(line_key(leg["line"]))
        if total == line:
            return "VOID"  # push on integer lines
        went = "OVER" if total > line else "UNDER"
        hit = leg["outcome"] == went
    else:
        hit = leg["outcome"] == winner
    if leg.get("side", "YES") == "NO":
        hit = not hit
    return "WON" if hit else "LOST"


def settle_bet(cfg, store, bet, winner: str, source: str, now: datetime,
               total: Decimal | None = None, fraction: Decimal | None = None) -> None:
    start = datetime.fromisoformat(bet["event_start_utc"]) \
        if bet["event_start_utc"] else None
    # future-start guard: refuse to settle an event that hasn't started (§11.1)
    if start and start > now:
        log.warning("refusing to settle future event for bet %d", bet["id"])
        return
    # terminal only once the event genuinely ended (grace, §10.4)
    if start and now < start + timedelta(
            minutes=int(cfg["tracker"]["terminal_mute_grace_mins"])):
        log.info("bet %d inside terminal grace; not settling yet", bet["id"])
        return
    legs = json.loads(bet["legs_json"])
    # PARTIAL never routes through _leg_result — it would record every YES leg
    # LOST, fiction that reconcile then compares venue verdicts against
    results = ["PARTIAL"] * len(legs) if winner == "PARTIAL" \
        else [_leg_result(leg, winner, total) for leg in legs]
    payload = {"result": results[0] if len(results) == 1 else results,
               "winner": winner, "source": source,
               "per_leg": results}
    if fraction is not None:
        payload["fraction"] = str(fraction)  # UMA fractional settlement (v3.4)
    store.insert("bet_events", {
        "bet_id": bet["id"], "created_at": now.isoformat(), "kind": "settled",
        "payload_json": json.dumps(payload)})

    placed = datetime.fromisoformat(bet["created_at"])
    # instant-settle tripwire: resolution-settled kinds only (§10.4)
    if (now - placed).total_seconds() < float(cfg["settle_tripwire_secs"]):
        store.insert("annotations", {
            "created_at": now.isoformat(), "target_kind": "bet",
            "target_id": str(bet["id"]), "kind": "settle-tripwire",
            "detail": "settled within one poll interval of placement"})
        store.insert("blacklist", {
            "fingerprint": bet["fingerprint"], "event_key": bet["event_key"],
            "reason": "settle-tripwire", "created_at": now.isoformat()})
    # timing guard: placed after the event started (defense in depth, §11.1)
    if start and placed > start:
        store.insert("annotations", {
            "created_at": now.isoformat(), "target_kind": "bet",
            "target_id": str(bet["id"]), "kind": "INVALID_AFTER_FINAL",
            "detail": f"placed {placed.isoformat()} after start {start.isoformat()}"})


def settle_manual(cfg, store, event_key: str, result: str,
                  now: datetime | None = None) -> int:
    """`arb settle --manual EVENT RESULT`. RESULT = winning outcome token,
    'VOID', or 'PARTIAL:<fraction>'. Appends immutable rows — a typo is forever."""
    now = now or datetime.now(timezone.utc)
    fraction = None
    winner = result
    if result.upper().startswith("PARTIAL:"):
        winner, fraction = "PARTIAL", Decimal(result.split(":", 1)[1])
    matched = [b for b in open_resolution_bets(store)
               if b["event_key"] == event_key]
    if matched and winner not in ("VOID", "PARTIAL"):
        outcomes = {leg.get("outcome") for b in matched
                    for leg in json.loads(b["legs_json"])}
        if winner not in outcomes:
            # legitimate for single-leg bets (opponent wins) — but scream, a
            # typo'd token silently settles every YES leg LOST, forever (M6)
            log.warning("settle --manual %s: winner %r matches no leg outcome "
                        "%s — verify spelling before trusting this settlement",
                        event_key, winner, sorted(o for o in outcomes if o))
    n = 0
    for bet in matched:
        settle_bet(cfg, store, bet, winner, "manual", now, fraction=fraction)
        n += 1
    return n


def settle_auto_mlb(cfg, store, now: datetime | None = None) -> int:
    """MLB Stats API is the primary MLB settlement oracle (free, unlimited)."""
    from arb.matching import load_aliases, normalize_team
    from arb.venues.mlbstats import fetch_schedule, is_final
    now = now or datetime.now(timezone.utc)
    aliases = load_aliases()
    bets = [b for b in open_resolution_bets(store)
            if b["sport"] == "baseball_mlb" and b["event_start_utc"]]
    if not bets:
        return 0
    dates = sorted({datetime.fromisoformat(b["event_start_utc"]).astimezone(NY)
                    .date().isoformat() for b in bets})
    finals = []
    for d in dates:
        try:
            finals.extend(g for g in fetch_schedule(cfg, d) if is_final(g))
        except Exception as e:  # noqa: BLE001
            log.warning("mlb schedule fetch failed for %s: %s", d, e)
    window = timedelta(hours=int(cfg["settle_doubleheader_window_hours"]))
    n = 0
    for bet in bets:
        start = datetime.fromisoformat(bet["event_start_utc"])
        pair = set(bet["event_key"].split(":")[2:4])
        scored = []  # (ha, aa, game) for same-pair finals with a final score
        for g in finals:
            ha = normalize_team("baseball_mlb", g["home"], aliases)
            aa = normalize_team("baseball_mlb", g["away"], aliases)
            if not ha or not aa or {ha, aa} != pair:
                continue
            if g["home_score"] is None or g["away_score"] is None:
                continue
            scored.append((ha, aa, g))
        if not scored:
            continue
        bet_pk = bet["gamepk"]
        if bet_pk is not None:
            # gamePk is authoritative — settle the exact game, ignore the window
            chosen = next((t for t in scored if t[2]["gamePk"] == bet_pk), None)
            if chosen is None:
                continue  # no final for this game yet
        else:
            in_window = [t for t in scored
                         if abs(t[2]["start_utc"] - start) <= window]
            if not in_window:
                continue
            if len(in_window) > 1:
                # doubleheader with no gamePk to disambiguate — refuse, don't
                # guess (rule 3 ethos). One annotation per ambiguous bet.
                store.insert("annotations", {
                    "created_at": now.isoformat(), "target_kind": "bet",
                    "target_id": str(bet["id"]), "kind": "ambiguous-doubleheader",
                    "detail": f"{len(in_window)} same-pair finals within "
                              f"{cfg['settle_doubleheader_window_hours']}h; no gamePk"})
                log.warning("bet %s: ambiguous doubleheader (%s finals in window), "
                            "refusing to settle", bet["id"], len(in_window))
                continue
            chosen = in_window[0]
        ha, aa, g = chosen
        winner = ha if g["home_score"] > g["away_score"] else aa
        total = Decimal(g["home_score"] + g["away_score"])
        settle_bet(cfg, store, bet, winner, f"mlbstats:{g['gamePk']}", now,
                   total=total)
        n += 1
    return n
