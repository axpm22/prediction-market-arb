"""L14 resolution reader and Polymarket-native settlement (spec §5 step 5, §10).

Rule 1: one public, unauthenticated Gamma GET per (at most) two markets.
Rule 3: every field below was read off a LIVE resolved market on 2026-09-03
        (Task 4 step 0, fixture tests/fixtures/whale_gamma_resolved.json, rows
        in data/verified_facts.md). `outcomePrices` is a JSON-encoded STRING
        holding the exact one-character strings "0" and "1"; the winner is the
        index of the "1", and `clobTokenIds`/`outcomes` share that index.
        A closed market whose prices are anything else (`["0","0"]` and
        continuous prices both observed) is NOT resolved here — it stays open
        and eventually gives up with an annotation, never a guessed winner.
Rule 9: settlement appends `bet_events` rows through settle_bet; the tape file
        (resolutions, meta) is a different database entirely.

The same reader serves the scorer (Task 5+): one verified field set drives both
whale outcomes and bet settlement, so a field change breaks both loudly.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from arb.settlement import open_resolution_bets, settle_bet
from arb.venues.base import get_json
from arb.whale_backfill import _json_list
from arb.whale_tape import (GAMMA, GAMMA_IDS_PER_CALL, _backoff_active, _bucket,
                            _call, _iso, _meta, _set_backoff, _set_meta, log)

# The only two price strings a resolution ever names (verified 2026-09-03).
WINNER_PRICE, LOSER_PRICE = "1", "0"
VOID_PRICES = ("0.5", "0.5")


@dataclass(frozen=True)
class Resolution:
    condition_id: str
    status: str                        # resolved | void
    winner_outcome_index: int | None
    resolved_at: str | None
    market: dict                       # the Gamma row as recorded

    def token(self) -> str | None:
        i = self.winner_outcome_index
        toks = _json_list(self.market.get("clobTokenIds"))
        return toks[i] if i is not None and i < len(toks) else None

    def label(self) -> str | None:
        i = self.winner_outcome_index
        outs = _json_list(self.market.get("outcomes"))
        return outs[i] if i is not None and i < len(outs) else None


def parse_resolution(market: dict) -> tuple[str, int | None]:
    """(status, winner_outcome_index) for ONE Gamma market row.
    resolved -> exactly one "1" against one "0"; void -> 0.5/0.5, or a void
    entry in umaResolutionStatuses (that value has NEVER been observed — it is
    a conservative extra trigger that can only ever route to VOID, never name a
    winner); anything else, closed or not, is still open."""
    if not market.get("closed"):
        return "open", None
    prices = [str(p) for p in _json_list(market.get("outcomePrices"))]
    statuses = [str(s).lower() for s in _json_list(market.get("umaResolutionStatuses"))]
    if tuple(prices) == VOID_PRICES or any("void" in s for s in statuses):
        return "void", None
    if sorted(prices) == [LOSER_PRICE, WINNER_PRICE]:
        return "resolved", prices.index(WINNER_PRICE)
    return "open", None


def resolution_for(con, condition_id: str) -> Resolution | None:
    """The persisted verdict, or None while the market is still open."""
    row = con.execute("SELECT * FROM resolutions WHERE condition_id=?",
                      (condition_id,)).fetchone()
    if row is None:
        return None
    return Resolution(row["condition_id"], row["status"], row["winner_outcome_index"],
                      row["resolved_at"], json.loads(row["raw_json"] or "{}"))


def _due(con, cfg, condition_ids, now) -> list[str]:
    """Ids worth a call: nothing is read before resolve_after_hours past the
    market's start, and a market given up on today is not retried until
    tomorrow. An id with no `markets` row rides on the caller's own start
    (settle_auto_pm passes the bet's event_start_utc)."""
    after = timedelta(hours=float(cfg["whale"]["resolve_after_hours"]))
    day = now.strftime("%Y-%m-%d")
    due = []
    for cond in dict.fromkeys(condition_ids):
        if _meta(con, f"giveup:{cond}") == day:
            continue
        row = con.execute("SELECT start_utc FROM markets WHERE condition_id=?",
                          (cond,)).fetchone()
        start = _iso(row["start_utc"]) if row else None
        if start and datetime.fromisoformat(start) + after > now:
            continue
        if resolution_for(con, cond) is None:
            due.append(cond)
    return due


def read_resolutions(cfg, con, condition_ids, now, get=get_json) -> int:
    """Persist one `resolutions` row per market Gamma reports resolved or void.
    Open markets persist nothing, so they are simply re-read next cycle."""
    ids = _due(con, cfg, condition_ids, now)
    if not ids or _backoff_active(con, now):
        return 0
    bucket = _bucket(cfg)
    n = 0
    for i in range(0, len(ids), GAMMA_IDS_PER_CALL):
        chunk = ids[i:i + GAMMA_IDS_PER_CALL]
        # repeated-param form + closed=true: the ONLY query verified to reach a
        # resolved market (2026-09-03); the comma CSV matches nothing.
        url = (GAMMA + "/markets?"
               + "&".join(f"condition_ids={c}" for c in chunk) + "&closed=true")
        try:
            body = _call(bucket, get, url, cfg, {})
        except Exception as e:  # noqa: BLE001
            log.warning("whale resolution read aborted: %s", e)
            _set_backoff(con, cfg, now)
            break
        for m in body:
            cond = str(m.get("conditionId") or "")
            status, idx = parse_resolution(m)
            if cond not in chunk or status == "open":
                continue
            con.execute(
                "INSERT OR IGNORE INTO resolutions (condition_id, resolved_at,"
                " winner_outcome_index, status, source, raw_json, fetched_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (cond, _iso(m.get("closedTime")) or now.isoformat(), idx, status,
                 "gamma", json.dumps(m), now.isoformat()))
            n += 1
        con.commit()
    return n


def _winner_label(bet, leg, res: Resolution) -> str | None:
    """The outcome label settle_bet compares against `leg["outcome"]`. Decided
    by TOKEN, which is the resolution's own identifier, then translated to the
    label the leg carries — our leg labels are normalised, Gamma's are not."""
    if res.status == "void":
        return "VOID"
    token = str(leg.get("venue_market_id") or "").partition(":")[2]
    if token and token == res.token():
        return leg["outcome"]          # our side won, whatever it is called
    if res.label() != leg["outcome"]:
        return res.label()             # our side lost: name the other outcome
    log.warning("bet %s: token %s lost but its label %r equals the winner's — "
                "refusing to settle", bet["id"], token, leg["outcome"])
    return None


def _pm_leg(bet) -> dict | None:
    """The bet's single Polymarket leg. A WHALE bet has exactly one (§8); one
    market's winner applied to a multi-leg row would settle the other legs off
    the wrong oracle, so refuse loudly rather than guess."""
    legs = json.loads(bet["legs_json"])
    if len(legs) == 1 and str(legs[0].get("venue_market_id") or "").startswith("0x"):
        return legs[0]
    log.warning("bet %s: not one Polymarket leg — not settled by pm-gamma", bet["id"])
    return None


def settle_auto_pm(cfg, store, con, now: datetime | None = None, get=get_json) -> int:
    """Settle every open WHALE bet whose market Gamma reports resolved (§10).
    Never raises on a tape problem: `settle` must reach reconcile() and the
    invariant check() whatever this lane's own database is doing."""
    now = now or datetime.now(timezone.utc)
    pairs = [(b, _pm_leg(b)) for b in open_resolution_bets(store)
             if b["kind"] == "WHALE"]
    bets = [b for b, leg in pairs if leg]
    if not bets:
        return 0
    legs = {b["id"]: leg for b, leg in pairs if leg}
    conds = {i: str(leg["venue_market_id"]).partition(":")[0]
             for i, leg in legs.items()}
    try:
        _seed_starts(con, bets, conds, now)
        read_resolutions(cfg, con, list(conds.values()), now, get=get)
        n = 0
        for bet in bets:
            cond = conds[bet["id"]]
            res = resolution_for(con, cond)
            if res is None:
                _giveup(cfg, store, con, bet, cond, now)
                continue
            winner = _winner_label(bet, legs[bet["id"]], res)
            if winner is None:
                continue
            settle_bet(cfg, store, bet, winner, f"pm-gamma:{cond}", now)
            n += 1
        return n
    except sqlite3.OperationalError as e:
        log.warning("whale tape unavailable, settled 0 WHALE bets: %s", e)
        return 0


def _seed_starts(con, bets, conds, now):
    """A bet whose market the tape never saw (a wiped or foreign tape) still
    has to become due and to give up: carry the bet's own start across."""
    for bet in bets:
        cond = conds[bet["id"]]
        if not cond or not bet["event_start_utc"]:
            continue
        con.execute("INSERT OR IGNORE INTO markets (condition_id, start_utc, "
                    "first_seen, last_seen) VALUES (?,?,?,?)",
                    (cond, _iso(bet["event_start_utc"]), now.isoformat(),
                     now.isoformat()))
    con.commit()


def _giveup(cfg, store, con, bet, cond, now):
    """resolve_giveup_days past the start with no resolution: ONE annotation in
    the ledger, then at most one retry a day. Never a guessed winner (rule 3)."""
    start = _iso(bet["event_start_utc"])
    if not start:
        return
    if datetime.fromisoformat(start) + timedelta(
            days=int(cfg["whale"]["resolve_giveup_days"])) > now:
        return
    key = f"giveup:{cond}"
    if _meta(con, key) is None:
        store.insert("annotations", {
            "created_at": now.isoformat(), "target_kind": "bet",
            "target_id": str(bet["id"]), "kind": "resolution-giveup",
            "detail": f"{cond} unresolved {cfg['whale']['resolve_giveup_days']}d "
                      f"after start {start}"})
        log.warning("bet %s: giving up on Polymarket resolution for %s",
                    bet["id"], cond)
    _set_meta(con, key, now.strftime("%Y-%m-%d"))
