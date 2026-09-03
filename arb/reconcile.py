"""Per-venue resolution cross-check (SPEC §11.2, bug A2). One resolver per venue,
composed by RESOLVERS. Settlement stays `provisional` in every surface until each
leg's venue confirms. Only THIS leg's venue returning None keeps the leg
unresolved — a whole bet is never skipped for one venue (V1's structural no-op)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.logutil import get_logger

log = get_logger("arb.reconcile", "logs/scan.log")


def _kalshi(cfg, leg: dict):
    """Market result via the public market endpoint. '' = unresolved."""
    from arb.venues.base import get_json
    ticker = leg.get("venue_market_id")
    if not ticker:
        return None
    m = get_json(
        f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}", cfg
    ).get("market", {})
    result = m.get("result")
    if result not in ("yes", "no"):
        return None
    won = (result == "yes") == (leg.get("venue_side", "YES") == "YES")
    return "WON" if won else "LOST"


def _polymarket(cfg, leg: dict):
    """UMA resolution via public Gamma reads — None until finality past the
    dispute window. Post-resolution outcomePrices pin 0/1 (or fractions)."""
    from arb.venues.base import get_json
    vmid = leg.get("venue_market_id") or ""
    condition_id = vmid.split(":")[0]
    if not condition_id.startswith("0x"):
        return None
    # `closed=true` is load-bearing, not a filter for tidiness: gamma defaults
    # this endpoint to closed=false, so without it the query returns 0 rows for
    # exactly the RESOLVED markets this resolver exists to read (verified
    # 2026-07-27 — same condition_id, 0 rows without, 1 row with). Every
    # Polymarket leg silently stayed unresolved, which starved the cross-venue
    # settlement-agreement audit of its only input. An open market still
    # returns 0 rows here, so the None-until-final contract is unchanged.
    markets = get_json(
        f"https://gamma-api.polymarket.com/markets?condition_ids={condition_id}"
        f"&closed=true", cfg)
    if not markets or not markets[0].get("closed"):
        return None
    m = markets[0]
    try:
        prices = [Decimal(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        outcomes = json.loads(m.get("outcomes") or "[]")
    except Exception:  # noqa: BLE001
        return None
    token = vmid.split(":")[1] if ":" in vmid else None
    tokens = json.loads(m.get("clobTokenIds") or "[]")
    if token is None or token not in tokens:
        return None
    p = prices[tokens.index(token)]
    if p == 1:
        return "WON"
    if p == 0:
        return "LOST"
    return ("PARTIAL", p)  # UMA fractional settlement (v3.4)


def _sx(cfg, leg: dict):
    """SX settled-status semantics UNVERIFIED (data/verified_facts.md): always
    None until a live settlement verifies the status value — SX legs cannot
    leave provisional/REVIEW, stated rather than silent."""
    return None


RESOLVERS = {"kalshi": _kalshi, "polymarket": _polymarket, "sx": _sx}


def _reconciled_legs(store, bet_id: int) -> set:
    """Leg indices already carrying a non-None verdict across ALL prior
    `reconciled` events for this bet (union). A bet is done only when this
    covers every leg — the V1 no-op stopped after the first pass (H4)."""
    covered = set()
    for e in store.query(
            "SELECT payload_json FROM bet_events WHERE bet_id=? AND kind='reconciled'",
            (bet_id,)):
        for i, v in enumerate(json.loads(e["payload_json"]).get("verdicts", [])):
            if v is not None:
                covered.add(i)
    return covered


def reconcile(cfg, store, now: datetime | None = None) -> int:
    """Runs automatically after every settle cycle. Appends one `reconciled`
    bet_event per pass with the newly-resolved leg verdicts; corrections append
    rows, never edit. Re-runs a bet until every leg has a verdict (per-leg, H4)."""
    now = now or datetime.now(timezone.utc)
    # kind WHALE is excluded, not skipped later: single venue, single oracle —
    # the Polymarket resolution the bet settled from IS its reconciliation, so a
    # cross-check would re-read the same Gamma row to compare it with itself.
    settled = store.query(
        """SELECT fb.* FROM flat_bets fb
           WHERE fb.kind != 'WHALE'
             AND EXISTS (SELECT 1 FROM bet_events be
                         WHERE be.bet_id = fb.id AND be.kind = 'settled')""")
    n = 0
    for bet in settled:
        legs = json.loads(bet["legs_json"])
        covered = _reconciled_legs(store, bet["id"])
        if len(covered) >= len(legs):
            continue  # every leg already reconciled — nothing left to do
        sev = store.query(
            "SELECT created_at, payload_json FROM bet_events "
            "WHERE bet_id=? AND kind='settled'", (bet["id"],))[0]
        settled_at = datetime.fromisoformat(sev["created_at"]) \
            if sev["created_at"] else None
        verdicts = []
        for i, leg in enumerate(legs):
            resolver = RESOLVERS.get(leg.get("venue", ""))
            if i in covered:
                verdicts.append(None)  # already reconciled
                continue
            v = None
            if resolver is not None:
                try:
                    v = resolver(cfg, leg)
                except Exception as e:  # noqa: BLE001
                    log.warning("resolver %s failed for bet %d: %s",
                                leg.get("venue"), bet["id"], e)
            if v is None and settled_at is not None:
                # D5 give-up bound: past the venue's settlement lag plus grace,
                # a still-None leg records UNRESOLVABLE (covered — never
                # re-fetched). SX legs were re-fetched every pass forever.
                lag_h = float(cfg["venue_settlement_lag_hours"]
                              .get(leg.get("venue"), 0))
                if now > settled_at + timedelta(
                        hours=lag_h, days=int(cfg["reconcile_give_up_days"])):
                    v = "UNRESOLVABLE"
                    log.info("bet %s leg %s: giving up reconcile (%s, settled %s)",
                             bet["id"], i, leg.get("venue"), sev["created_at"])
            verdicts.append(v)
        if all(v is None for v in verdicts):
            continue  # no NEW leg resolved this pass; stays provisional
        store.insert("bet_events", {
            "bet_id": bet["id"], "created_at": now.isoformat(), "kind": "reconciled",
            "payload_json": json.dumps({
                "verdicts": [v if not isinstance(v, tuple) else
                             {"partial": str(v[1])} for v in verdicts]})})
        n += 1
        # mismatch on a trusted pair reverts the pair to REVIEW immediately (§11.2)
        settled_payload = json.loads(sev["payload_json"])
        if "fraction" in settled_payload:
            continue  # UMA fractional settlement: venue verdicts are binary, a
            # PARTIAL bet can never evidence a settlement mismatch on the pair
        expected = settled_payload.get("per_leg") or []
        for i, v in enumerate(verdicts):
            if v is not None and i < len(expected) and not isinstance(v, tuple) \
                    and v != "UNRESOLVABLE" and v != expected[i]:
                log.critical("reconcile MISMATCH bet %s leg %s: settled %s venue %s",
                             bet["id"], i, expected[i], v)
                store.insert("annotations", {
                    "created_at": now.isoformat(), "target_kind": "bet",
                    "target_id": str(bet["id"]), "kind": "settlement_mismatch",
                    "detail": f"leg {i}: scores said {expected[i]}, venue said {v}; "
                              f"pair {bet['venue_pair']} reverted to REVIEW"})
                # ENFORCE the revert: append a demotion so booking_trusted stops
                # trusting this pair (§11.2). Append-only; never un-demoted here.
                store.insert("pair_demotions", {
                    "pair": bet["venue_pair"], "created_at": now.isoformat(),
                    "reason": f"settlement_mismatch bet {bet['id']} leg {i}: "
                              f"scores {expected[i]} vs venue {v}"})
    return n


GRADED = ("WON", "LOST", "VOID")


def certificate_n(store, venue_pair: str) -> int:
    """Resolution Equivalence Certificate size (SPEC §12, §18.2): the number of
    XSPREAD positions on this venue pair whose BOTH legs graded and which
    settled complementary. Feeds eligibility's rule-of-three charge (3/n)*LGM,
    so it must count only positions that actually demonstrated agreement.

    Read but never written before now: arb/eligibility.py:77 returns
    'no-certificate' when this is 0, so every XSPREAD was review-lane forever
    regardless of configuration.

    Verdicts are UNIONED across passes. reconcile grades one leg per pass
    (_reconciled_legs), so a fully graded pair appears as ["WON", null] in one
    event and [null, "LOST"] in a later one; reading events independently
    reports it as two half-graded pairs and undercounts the certificate.

    UNRESOLVABLE is missing data, never evidence of agreement — which is why an
    SX pair can never accumulate a certificate: _sx returns None unconditionally
    while its settled-status semantics stay unverified.
    """
    per_bet: dict[int, dict[int, str]] = {}
    for r in store.query(
            "SELECT b.id AS bet_id, e.payload_json FROM flat_bets b "
            "JOIN bet_events e ON e.bet_id = b.id "
            "WHERE e.kind = 'reconciled' AND b.kind = 'XSPREAD' "
            "AND b.venue_pair = ? ORDER BY e.id", (venue_pair,)):
        verdicts = (json.loads(r["payload_json"]) or {}).get("verdicts") or []
        slot = per_bet.setdefault(r["bet_id"], {})
        for i, v in enumerate(verdicts):
            if v in GRADED:
                slot[i] = v
    n = 0
    for slot in per_bet.values():
        if len(slot) < 2:
            continue                                    # not both legs graded
        if list(slot.values()).count("WON") != 1:
            continue                                    # a cover pays exactly once
        n += 1
    return n
