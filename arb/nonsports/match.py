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

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_STOP = frozenset("a an and at be by for in of on or the to will you what "
                  "which who".split())


def load_concepts(path: str | Path = CONCEPTS_PATH) -> dict:
    """{concept: [[token, ...], ...]} — a text hits the concept if ALL tokens
    of any one phrase appear in it. Curated, auditable, human-extendable."""
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
    return {Decimal(m.replace(",", "")) for m in _NUM.findall(text)}


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
                     concepts: dict) -> list[dict]:
    window_days = int(cfg["nonsports"]["match_date_window_days"])
    cap = int(cfg["nonsports"]["max_candidates"])
    by_concept: dict[str, list[dict]] = {}
    for r in p_rows:
        for c in concepts_of(r["text"], concepts):
            by_concept.setdefault(c, []).append(r)
    out = []
    for k in k_rows:
        if k["date"] is None:
            continue
        for concept in concepts_of(k["text"], concepts):
            for p in by_concept.get(concept, []):
                if p["date"] is None:
                    continue
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
