"""Kalshi mutually-exclusive event baskets (negrisk.kalshi_events — declared
in config since v3.5, implemented 2026-07-14 to close the venue imbalance:
three lanes scanned Polymarket wholesale while Kalshi's ~3k non-sports series
were only touched via 9 mapped pairs).

Discovery: the global open-markets listing (verified 2026-07-14,
data/verified_facts.md) -> keep markets whose series belongs to a non-sports
catalog category -> group by event_ticker -> groups of 3..max_outcomes
two-sided markets -> /events/{ticker} supplies the mutually_exclusive flag
(exhaustiveness proof condition a) -> negrisk.scan_group does the math.
Every observed book persists (rule 7) whether or not a basket flags."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

from arb.logutil import get_logger
from arb.negrisk import quote_rows, scan_group
from arb.venues.base import Deadline, get_json
from arb.venues.kalshi import BASE, fetch_event

log = get_logger("arb.kalshi_events", "logs/scan.log")

_CAT_FEE = {"Politics": "politics", "Economics": "econ",
            "Climate and Weather": "weather"}


def _series_categories(cfg, deadline) -> dict[str, str]:
    out = {}
    for cat in cfg["nonsports"]["kalshi_categories"]:
        data = get_json(f"{BASE}/series?category={quote(str(cat))}", cfg,
                        deadline=deadline)
        for s in data.get("series", []):
            out[s["ticker"]] = _CAT_FEE.get(str(cat), "politics")
    return out


def _open_markets(cfg, deadline) -> list[dict]:
    markets, cursor, pages = [], "", 0
    cap = int(cfg["negrisk"]["scan_pages"])
    while pages < cap:
        if deadline.expired():
            log.warning("kalshi ME discovery truncated by deadline at page %s", pages)
            break
        url = f"{BASE}/markets?status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        data = get_json(url, cfg, deadline=deadline)
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor") or ""
        pages += 1
        if not cursor:
            break
    else:
        if cursor:
            log.warning("kalshi ME discovery pagination cap hit; coverage truncated")
    return markets


def _dollars(m, key) -> Decimal | None:
    v = m.get(key)
    if v is None:
        return None
    d = Decimal(str(v))
    return d if 0 < d < 1 else None  # sentinel 0/1 prices are not quotes


def _outcomes(group: list[dict]) -> list[dict] | None:
    outs = []
    for m in group:
        ya, yb = _dollars(m, "yes_ask_dollars"), _dollars(m, "yes_bid_dollars")
        na, nb = _dollars(m, "no_ask_dollars"), _dollars(m, "no_bid_dollars")
        if ya is None or yb is None:
            return None  # one-sided outcome: group fails proof condition (c)
        size = m.get("yes_ask_size_fp")
        outs.append({"label": m.get("yes_sub_title") or m.get("subtitle")
                     or m.get("ticker", ""),
                     "yes_ask": ya, "yes_bid": yb, "no_ask": na, "no_bid": nb,
                     "depth_usd": Decimal(str(size)) * ya
                     if size is not None else None,
                     "volume_24h": Decimal(str(m["volume_24h_fp"]))
                     if m.get("volume_24h_fp") is not None else None})
    return outs


def scan_kalshi_events(cfg, now: datetime) -> tuple[list[dict], list[dict], int]:
    """-> (quote_rows, baskets, n_groups_checked)."""
    deadline = Deadline(cfg["execution"]["venue_deadline_secs"])
    series_cat = _series_categories(cfg, deadline)
    groups: dict[str, list[dict]] = {}
    for m in _open_markets(cfg, deadline):
        et = m.get("event_ticker") or ""
        if series_cat.get(et.split("-")[0]) is None:
            continue  # not a non-sports series
        groups.setdefault(et, []).append(m)
    cands = {et: ms for et, ms in groups.items()
             if 3 <= len(ms) <= int(cfg["negrisk"]["max_outcomes"])}
    # largest groups first — where exhaustive baskets live
    ordered = sorted(cands.items(), key=lambda kv: -len(kv[1]))
    cap = int(cfg["negrisk"]["kalshi_event_checks"])
    quotes, baskets, checked = [], [], 0
    for et, ms in ordered[:cap]:
        if deadline.expired():
            log.warning("kalshi ME scan truncated by deadline at %s groups", checked)
            break
        outs = _outcomes(ms)
        cat = series_cat[et.split("-")[0]]
        event_key = f"nonsports:kalshi:{et}"
        if outs is None:
            continue  # one-sided books; nothing two-sided to persist coherently
        quotes.extend(quote_rows(cfg, "kalshi", cat, event_key, outs, now))
        try:
            me = bool(fetch_event(cfg, et).get("mutually_exclusive"))
        except Exception as e:  # noqa: BLE001 — one event fetch never kills the scan
            log.warning("kalshi event %s fetch failed: %s", et, e)
            continue
        checked += 1
        bs = scan_group(cfg, "kalshi", cat, event_key,
                        ms[0].get("title", et), outs, venue_flag=me)
        exp = min((m.get("expected_expiration_time") or m.get("close_time") or ""
                   for m in ms), default="") or None
        for b in bs:
            b["resolution_date"] = exp  # D2 horizon gate input
        baskets.extend(bs)
    return quotes, baskets, checked
