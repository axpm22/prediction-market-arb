"""Mapped-pair cross-venue lane: the consumer of data/nonsports_map.yaml
(rule 11's trusted MATCHING path — positions stay XSPREAD/REVIEW until the
§18.2 gate; classify() enforces that, this module never decides lanes).

Per adjudicated pair, fetch both venues' books, persist every quote under the
shared event_key (rule 7), and emit locked cross-venue candidates
(K.YES+PM.NO and K.NO+PM.YES summing < 1 after fees) plus divergence signals.
Inverted pairs are normalized to the Kalshi framing: the PM YES token becomes
side NO and vice versa; venue_side keeps the venue's own labeling so
reconcile can still talk to the venue."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

from arb.logutil import get_logger
from arb.models import Event, Opportunity, Quote, q6
from arb.venues.base import get_json, post_json
from arb.venues.fees import leg_fee_cost

log = get_logger("arb.mapped", "logs/scan.log")
MAP_PATH = Path("data/nonsports_map.yaml")
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

_ECON = {"cpi", "unemployment", "gdp", "recession", "jobless_claims",
         "retail_sales", "payrolls", "fed_decision", "gas_price"}


def _fee_category(concept: str) -> str:
    if concept in _ECON:
        return "econ"
    if "temp" in concept or concept in ("named_storm", "earthquake"):
        return "weather"
    return "politics"


def load_map(path: str | Path = MAP_PATH) -> list[dict]:
    if not Path(path).exists():
        return []
    return yaml.safe_load(Path(path).read_text()) or []


def _quote(cfg, entry, venue, side, venue_side, cost, bid, depth, cat, now, vmid):
    return Quote(
        event_key=entry["event_key"], venue=venue, book=venue,
        market_type="WINNER", line=None, outcome_label="prop", side=side,
        venue_side=venue_side, raw_price=cost, cost=cost,
        fee_cost=leg_fee_cost(cfg, venue, cat, cost),
        depth_usd=depth, ladder_json=None, live=False, fetched_at=now,
        category="nonsports", sport=entry.get("concept", ""), bid=bid,
        volume_24h=None, start_utc=None, start_grade="absent",
        venue_market_id=vmid)


def kalshi_pair_quotes(cfg, entry, now) -> list[Quote]:
    m = get_json(f"{KBASE}/markets/{entry['kalshi']}", cfg).get("market", {})
    if not m or m.get("status") not in (None, "open", "active"):
        return []
    cat = _fee_category(entry.get("concept", ""))
    out = []
    for side, ask_key, bid_key, size_key in (
            ("YES", "yes_ask_dollars", "yes_bid_dollars", "yes_ask_size_fp"),
            ("NO", "no_ask_dollars", "no_bid_dollars", "yes_bid_size_fp")):
        ask = m.get(ask_key)
        if ask is None:
            continue
        ask = Decimal(str(ask))
        if not (0 < ask < 1):
            continue  # empty-book sentinel prices are not quotes
        size = m.get(size_key)
        out.append(_quote(cfg, entry, "kalshi", side, side, ask,
                          Decimal(str(m[bid_key])) if m.get(bid_key) else None,
                          Decimal(str(size)) * ask if size is not None else None,
                          cat, now, entry["kalshi"]))
    return out


def pm_pair_quotes(cfg, entry, now) -> list[Quote]:
    mkts = get_json(f"{GAMMA}/markets?condition_ids={entry['polymarket']}", cfg)
    if not mkts or mkts[0].get("closed"):
        return []
    tokens = json.loads(mkts[0].get("clobTokenIds") or "[]")
    if len(tokens) != 2:
        return []
    books = {b["asset_id"]: b for b in post_json(
        f"{CLOB}/books", cfg, [{"token_id": t} for t in tokens])
        if b and b.get("asset_id")}
    cat = _fee_category(entry.get("concept", ""))
    inverted = bool(entry.get("inverted"))
    out = []
    for token, venue_side in zip(tokens, ("YES", "NO")):
        book = books.get(token)
        if not book or not book.get("asks"):
            continue
        ask = min(Decimal(a["price"]) for a in book["asks"])
        size = [a for a in book["asks"] if Decimal(a["price"]) == ask][0]["size"]
        bid = max((Decimal(b["price"]) for b in book.get("bids") or []),
                  default=None)
        # normalize to the Kalshi framing: inverted flips the SIDE label only
        side = venue_side if not inverted else ("NO" if venue_side == "YES"
                                                else "YES")
        out.append(_quote(cfg, entry, "polymarket", side, venue_side, ask, bid,
                          Decimal(size) * ask, cat, now,
                          f"{entry['polymarket']}:{token}"))
    return out


def detect_pair(cfg, entry, quotes: list[Quote]) -> list[Opportunity]:
    """Locked cross-venue covers on the normalized proposition: YES+NO < 1
    across venues -> XSPREAD (classify() quarantines it); plus the divergence
    signal on same-side mids."""
    best: dict[tuple, Quote] = {}
    for q in quotes:
        k = (q.venue, q.side)
        if k not in best or q.effective_cost < best[k].effective_cost:
            best[k] = q
    event = Event(event_key=entry["event_key"], category="nonsports",
                  sport=entry.get("concept", ""), home="", away="",
                  start_utc=None, start_grade="absent")
    opps = []
    for a, b in ((("kalshi", "YES"), ("polymarket", "NO")),
                 (("kalshi", "NO"), ("polymarket", "YES"))):
        if a in best and b in best:
            legs = [best[a], best[b]]
            s = sum(q.effective_cost for q in legs)
            if s < 1:
                roi = q6(1 / s - 1)
                opps.append(Opportunity(
                    kind="XSPREAD", category="nonsports", event=event,
                    legs=legs, edge=q6(1 - s), stakes=[],
                    status="REVIEW" if roi > cfg["arb_stale_warn"] else "OK",
                    notes=f"mapped-pair {entry['kalshi']}; sum={q6(s)}"
                          + ("; inverted" if entry.get("inverted") else "")))
    for side in ("YES", "NO"):
        a, b = best.get(("kalshi", side)), best.get(("polymarket", side))
        if a and b and a.bid is not None and b.bid is not None:
            spread_a, spread_b = a.cost - a.bid, b.cost - b.bid
            if max(spread_a, spread_b) >= cfg["zombie_spread_min"]:
                continue  # a near-empty book makes any "mid" fiction, not signal
            mid_a, mid_b = (a.cost + a.bid) / 2, (b.cost + b.bid) / 2
            if abs(mid_a - mid_b) >= cfg["divergence_threshold"]:
                opps.append(Opportunity(
                    kind="DIVERGENCE", category="nonsports", event=event,
                    legs=[a, b], edge=q6(abs(mid_a - mid_b)), stakes=[],
                    status="REVIEW",
                    notes=f"mapped-pair {side} mids; spreads "
                          f"k={q6(spread_a)} pm={q6(spread_b)}"))
                break  # one divergence signal per pair per poll is enough
    return opps


def _pair_banned(store, event_key: str) -> bool:
    """Rule 11: a settlement mismatch permanently bans the matched pair. The
    mismatch annotation targets the bet; the ban keys off the pair's
    event_key and is append-only by construction (annotations never delete)."""
    return bool(store.query(
        """SELECT 1 FROM annotations a JOIN flat_bets fb
             ON fb.id = CAST(a.target_id AS INTEGER)
           WHERE a.target_kind='bet' AND a.kind='settlement_mismatch'
             AND fb.event_key = ? LIMIT 1""", (event_key,)))


def run_mapped(cfg, store, now: datetime | None = None) -> dict:
    from arb.cycle import persist_poll
    from arb.tracker import process_poll
    now = now or datetime.now(timezone.utc)
    entries = load_map()
    all_quotes, all_opps, errors, banned = [], [], 0, 0
    for entry in entries:
        if _pair_banned(store, entry["event_key"]):
            banned += 1
            log.warning("mapped pair %s BANNED by settlement mismatch; skipped "
                        "permanently (rule 11)", entry["kalshi"])
            continue
        try:
            qs = kalshi_pair_quotes(cfg, entry, now) \
                + pm_pair_quotes(cfg, entry, now)
        except Exception as e:  # noqa: BLE001 — a venue outage is data
            log.warning("mapped pair %s fetch failed: %s", entry["kalshi"], e)
            errors += 1
            continue
        all_quotes.extend(qs)
        all_opps.extend(detect_pair(cfg, entry, qs))
    poll_id = persist_poll(cfg, store, "mapped", all_quotes,
                           {"kalshi": {"ok": errors == 0, "n": len(all_quotes),
                                       "latency_ms": 0},
                            "polymarket": {"ok": errors == 0, "n": 0,
                                           "latency_ms": 0}},
                           [], all_opps, [], started=now)
    process_poll(cfg, store, poll_id, "mapped", all_opps,
                 {"kalshi": {"ok": True}}, now=now)
    s = {"pairs": len(entries), "quotes": len(all_quotes),
         "opps": len(all_opps), "errors": errors, "banned": banned}
    log.info("mapped lane: %s", s)
    return s
