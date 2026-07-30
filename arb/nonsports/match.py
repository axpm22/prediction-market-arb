"""Deterministic blocking-key matcher (plan Phase 1.2 — rule 11 stage 0).

NO fuzzy text matching (the V1 lesson). A Kalshi/Polymarket pair becomes a
CANDIDATE — never a match — iff it shares a curated concept
(data/nonsports_concepts.yaml), the resolution dates sit within
nonsports.match_date_window_days, and, when both sides carry numeric strikes,
at least one strike agrees exactly. Candidates go to human curation
(data/nonsports_curation_protocol.md); confirmed pairs enter
nonsports_map.yaml. Strike extraction is deliberately generous (recall);
precision is the curator's job.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import yaml

CONCEPTS_PATH = Path("data/nonsports_concepts.yaml")
ENTITIES_PATH = Path("data/nonsports_entities.yaml")
SOURCES_PATH = Path("data/nonsports_sources.yaml")

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_STOP = frozenset("a an and at be by for in of on or the to will you what "
                  "which who".split())


def load_concepts(path: str | Path = CONCEPTS_PATH) -> dict:
    """{concept: [[token, ...], ...]} — a text hits the concept if ALL tokens
    of any one phrase appear in it. Curated, auditable, human-extendable."""
    return yaml.safe_load(Path(path).read_text()) or {}


def load_sources(path: str | Path = SOURCES_PATH) -> dict:
    """Same phrase format as concepts: {source: [[token, ...], ...]}."""
    return yaml.safe_load(Path(path).read_text()) or {}


def source_conflict(a_rules: str, b_rules: str, sources: dict) -> tuple | None:
    """(a_sources, b_sources) when BOTH venues name a settlement source and the
    sets are DISJOINT; None otherwise. Not a shape question and not a measurand
    question — two rulebooks can describe the same physical quantity and still
    settle off different published products, which is rule 6's stated main
    source of fake arbs.

    Decided in code because the model demonstrably does not decide it: shown
    both texts in full it called NWS-Climatological and Wunderground the same
    measurand at 0.92-0.95 (2026-07-30), violating its own prompt rule 4. The
    §18.5 precision of 1.0 never covered this — all nine ground-truth positives
    are econ pairs settling on one shared BLS release.

    One side silent is missing information, not evidence of divergence.
    """
    a, b = concepts_of(a_rules, sources), concepts_of(b_rules, sources)
    if a and b and not (a & b):
        return a, b
    return None


def load_entities(path: str | Path = ENTITIES_PATH) -> dict:
    """Same phrase format as concepts: {entity: [[token, ...], ...]}. Guard:
    when BOTH sides of a pair name entities and the sets are disjoint, the
    pair is rejected (China GDP is not South Korea GDP, whatever the strike)."""
    return yaml.safe_load(Path(path).read_text()) or {}


def _tokens(text: str) -> set[str]:
    return {t for t in "".join(c if c.isalnum() or c == "." else " "
                               for c in text.lower()).split()
            if t not in _STOP}


def concepts_of(text: str, concepts: dict) -> set[str]:
    toks = _tokens(text)
    return {name for name, phrases in concepts.items()
            if any(set(p) <= toks for p in phrases)}


def extract_strikes(text: str) -> set[Decimal]:
    # bare 4-digit years are not strikes: "in Jul 2026" paired every K exact
    # market with every PM bucket (207 spurious pairs, 2026-07-13)
    return {Decimal(m.replace(",", "")) for m in _NUM.findall(text)
            if not ("." not in m and "," not in m and len(m) == 4
                    and 1900 <= int(m) <= 2100)}


# Folded into the verdict-cache sha (ai.py) so a change in shape-decision
# SEMANTICS re-asks instead of serving stale verdicts — without deleting a row
# (rule 11: the cache is append-only). v1->v2 on 2026-07-30: shape 'unknown'
# stopped short-circuiting to 'different' and now consults the model, so every
# v1 'unknown' verdict was recorded under a rule that no longer holds.
# v2->v3 same day: the range regex learned '°', so temperature buckets that
# were 'unknown' now parse and decide in code.
# v3->v4: settlement-source conflict is now decided in code before the model is
# asked, so any v3 'same' recorded across divergent sources no longer holds.
SHAPE_VERSION = "shape-v4"

_REL_RES = [
    # The trailing unit is required — a bare "100-101" is as likely a date range
    # or a ticker as a bucket. '°' joined '%' on 2026-07-30: weather is the
    # densest surface carrying BOTH venues, and every temperature bucket was
    # falling through to 'unknown' and buying an API call to reach a verdict
    # code can settle (Kalshi writes "100-101°", Gamma "100-101°F").
    ("range", re.compile(r"(\d+(?:\.\d+)?)\s*[%°]?\s*(?:–|-)\s*(\d+(?:\.\d+)?)\s*[%°]")),
    ("gte", re.compile(r"(?:≥\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*%?\s*(?:\+|or more|or higher|or above))")),
    ("lte", re.compile(r"(?:≤\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*%?\s*(?:or less|or lower|or below))")),
    ("gt", re.compile(r"(?:>|above|more than|exceed[s]?)\s*(\d+(?:\.\d+)?)")),
    ("lt", re.compile(r"(?:<|below|less than|under)\s*(\d+(?:\.\d+)?)")),
    ("exact", re.compile(r"(?:exactly|be)\s*(\d+(?:\.\d+)?)\s*%")),
]


def parse_relation(text: str) -> tuple | None:
    """(op, strike[, hi]) from a market's proposition text; None = unparsed.
    Order matters: '0.6% or more' must hit gte before exact's bare 'be X%'."""
    for op, rx in _REL_RES:
        m = rx.search(text)
        if m:
            vals = [Decimal(g) for g in m.groups() if g is not None]
            return (op, *vals)
    return None


def shape_relation(a_text: str, b_text: str) -> str:
    """'identical' | 'inverted' | 'different' | 'unknown' from proposition
    SHAPE alone (boundary worlds are decidable in code — the AI matcher failed
    exactly here at 0.95 confidence, first eval 2026-07-13). Says nothing about
    source/measurand; that's the AI's half."""
    a, b = parse_relation(a_text), parse_relation(b_text)
    if a is None or b is None:
        return "unknown"
    if a == b:
        return "identical"
    # exact complements on the same published print
    inv = {("gt", "lte"), ("lte", "gt"), ("lt", "gte"), ("gte", "lt")}
    if (a[0], b[0]) in inv and a[1] == b[1]:
        return "inverted"
    return "different"  # any other shape pair has a boundary world that splits


def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def kalshi_rows(markets: list[dict], series_titles: dict[str, str]) -> list[dict]:
    """Normalize Kalshi market objects. `series_titles` maps series ticker ->
    series title — market titles omit the subject (e.g. the city), which lives
    at series level, so match text = series title + market title."""
    rows = []
    for m in markets:
        series = m.get("event_ticker", m.get("ticker", "")).split("-")[0]
        text = f"{series_titles.get(series, '')} | {m.get('title', '')}"
        strikes = {Decimal(str(m[k])) for k in ("floor_strike", "cap_strike")
                   if m.get(k) is not None}
        rows.append({
            "venue": "kalshi", "id": m.get("ticker", ""), "text": text,
            "rules": m.get("rules_primary") or "",
            "strikes": strikes or extract_strikes(m.get("title", "")),
            "date": _iso(m.get("expected_expiration_time") or m.get("close_time")),
            "source": "",  # series-level settlement_sources, filled by the scan
        })
    return rows


def pm_rows(events: list[dict]) -> list[dict]:
    """Normalize Gamma events to per-market rows. Strikes come from the
    question/groupItemTitle text (PM encodes them in prose)."""
    rows = []
    for e in events:
        ev_title = e.get("title", "")
        source = str(e.get("resolutionSource") or "")
        for m in e.get("markets", []):
            if not m.get("clobTokenIds"):
                continue
            q = f"{ev_title} | {m.get('groupItemTitle') or ''} {m.get('question', '')}"
            rows.append({
                "venue": "polymarket",
                "id": str(m.get("conditionId") or m.get("id")),
                "text": q, "rules": (m.get("description") or "")[:2000],
                "strikes": extract_strikes(q),
                "date": _iso(m.get("endDate")),
                "source": source,
                "neg_risk": bool(m.get("negRisk") or e.get("negRisk")),
            })
    return rows


def block_candidates(cfg, k_rows: list[dict], p_rows: list[dict],
                     concepts: dict, entities: dict | None = None) -> list[dict]:
    entities = entities if entities is not None else {}
    window_days = int(cfg["nonsports"]["match_date_window_days"])
    cap = int(cfg["nonsports"]["max_candidates"])
    by_concept: dict[str, list[dict]] = {}
    for r in p_rows:
        r["_ents"] = concepts_of(r["text"], entities)
        for c in concepts_of(r["text"], concepts):
            by_concept.setdefault(c, []).append(r)
    out = []
    for k in k_rows:
        if k["date"] is None:
            continue
        k_ents = concepts_of(k["text"], entities)
        for concept in concepts_of(k["text"], concepts):
            for p in by_concept.get(concept, []):
                if p["date"] is None:
                    continue
                if k_ents and p["_ents"] and not (k_ents & p["_ents"]):
                    continue  # both name entities, none shared: never a pair
                gap = abs((k["date"] - p["date"]).total_seconds()) / 86400
                if gap > window_days:
                    continue
                if k["strikes"] and p["strikes"] and not (k["strikes"] & p["strikes"]):
                    continue
                out.append({
                    "pair_id": f"{k['id']}|{p['id']}",
                    "concept": concept,
                    "kalshi_id": k["id"], "kalshi_text": k["text"],
                    "kalshi_rules": k["rules"][:1500],
                    "kalshi_date": k["date"].isoformat(),
                    "pm_id": p["id"], "pm_text": p["text"],
                    "pm_rules": p["rules"][:1500],
                    "pm_date": p["date"].isoformat(),
                    "strikes_kalshi": sorted(str(s) for s in k["strikes"]),
                    "strikes_pm_hit": sorted(str(s) for s in
                                             (k["strikes"] & p["strikes"])),
                    "date_gap_days": round(gap, 2),
                    "block_reason": f"concept={concept} gap={gap:.1f}d"
                                    + (" strike-match" if k["strikes"]
                                       & p["strikes"] else ""),
                })
    out.sort(key=lambda c: (c["concept"], c["date_gap_days"]))
    return out[:cap]
