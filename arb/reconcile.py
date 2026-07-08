"""Per-venue resolution cross-check (SPEC §11.2, bug A2). One resolver per venue,
composed by RESOLVERS. Settlement stays `provisional` in every surface until each
leg's venue confirms. Only THIS leg's venue returning None keeps the leg
unresolved — a whole bet is never skipped for one venue (V1's structural no-op)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
    markets = get_json(
        f"https://gamma-api.polymarket.com/markets?condition_ids={condition_id}", cfg)
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


def reconcile(cfg, store, now: datetime | None = None) -> int:
    """Runs automatically after every settle cycle. Appends one `reconciled`
    bet_event per (bet, venue) verdict; corrections append rows, never edit."""
    now = now or datetime.now(timezone.utc)
    settled = store.query(
        """SELECT fb.* FROM flat_bets fb
           WHERE EXISTS (SELECT 1 FROM bet_events be
                         WHERE be.bet_id = fb.id AND be.kind = 'settled')
             AND NOT EXISTS (SELECT 1 FROM bet_events be2
                             WHERE be2.bet_id = fb.id AND be2.kind = 'reconciled')""")
    n = 0
    for bet in settled:
        legs = json.loads(bet["legs_json"])
        verdicts = []
        for leg in legs:
            resolver = RESOLVERS.get(leg.get("venue", ""))
            if resolver is None:
                verdicts.append(None)
                continue
            try:
                verdicts.append(resolver(cfg, leg))
            except Exception as e:  # noqa: BLE001
                log.warning("resolver %s failed for bet %d: %s", leg.get("venue"),
                            bet["id"], e)
                verdicts.append(None)
        if all(v is None for v in verdicts):
            continue  # nothing confirmable yet; stays provisional
        store.insert("bet_events", {
            "bet_id": bet["id"], "created_at": now.isoformat(), "kind": "reconciled",
            "payload_json": json.dumps({
                "verdicts": [v if not isinstance(v, tuple) else
                             {"partial": str(v[1])} for v in verdicts]})})
        n += 1
        # mismatch on a trusted pair reverts the pair to REVIEW immediately (§11.2)
        settled_payload = json.loads(store.query(
            "SELECT payload_json FROM bet_events WHERE bet_id=? AND kind='settled'",
            (bet["id"],))[0]["payload_json"])
        expected = settled_payload.get("per_leg") or []
        for i, v in enumerate(verdicts):
            if v is not None and i < len(expected) and not isinstance(v, tuple) \
                    and v != expected[i]:
                log.critical("reconcile MISMATCH bet %d leg %d: settled %s venue %s",
                             bet["id"], i, expected[i], v)
                store.insert("annotations", {
                    "created_at": now.isoformat(), "target_kind": "bet",
                    "target_id": str(bet["id"]), "kind": "settlement_mismatch",
                    "detail": f"leg {i}: scores said {expected[i]}, venue said {v}; "
                              f"pair {bet['venue_pair']} must revert to REVIEW"})
    return n
