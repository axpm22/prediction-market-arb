"""Non-sports matcher scan (`arb nonsports`, plan Phase 1.2): fetch both
venues' open non-sports surface, run the deterministic blocker, and write
data/nonsports_candidates.yaml for human curation. The merge NEVER touches an
existing entry — curation verdicts are append-only truth."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import yaml

from arb.logutil import get_logger
from arb.nonsports.match import (block_candidates, concepts_of, kalshi_rows,
                                 load_concepts, pm_rows)
from arb.venues.base import Deadline, get_json

log = get_logger("arb.nonsports", "logs/scan.log")
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
CANDIDATES_PATH = Path("data/nonsports_candidates.yaml")


def _kalshi_side(cfg, concepts, deadline) -> list[dict]:
    titles: dict[str, str] = {}
    hits: list[str] = []
    for cat in cfg["nonsports"]["kalshi_categories"]:
        data = get_json(f"{KBASE}/series?category={quote(str(cat))}", cfg,
                        deadline=deadline)
        for s in data.get("series", []):
            titles[s["ticker"]] = s.get("title", "")
            if concepts_of(f"{s.get('title', '')} {s['ticker']}", concepts):
                hits.append(s["ticker"])
    cap = int(cfg["nonsports"]["max_series_fetch"])
    if len(hits) > cap:
        log.warning("kalshi concept-hit series %s exceed max_series_fetch %s; "
                    "truncating (extend the cap or the concept map)",
                    len(hits), cap)
    markets: list[dict] = []
    for st in hits[:cap]:
        if deadline.expired():
            log.warning("nonsports match deadline hit mid-kalshi; partial surface")
            break
        markets.extend(get_json(
            f"{KBASE}/markets?series_ticker={st}&status=open&limit=200", cfg,
            deadline=deadline).get("markets", []))
    return kalshi_rows(markets, titles)


def _pm_side(cfg, deadline) -> list[dict]:
    from arb.venues.polymarket import fetch_gamma_events
    events = []
    for tag in cfg["nonsports"]["polymarket_tags"]:
        events.extend(fetch_gamma_events(
            cfg, str(tag), max_pages=int(cfg["negrisk"]["scan_pages"]),
            deadline=deadline))
    return pm_rows(events)


def merge_candidates(path: Path, new: list[dict]) -> tuple[dict, int]:
    """Existing entries (and their curation verdicts) are never overwritten;
    only unseen pair_ids are added."""
    existing = yaml.safe_load(path.read_text()) or {} if path.exists() else {}
    added = 0
    for c in new:
        if c["pair_id"] not in existing:
            existing[c["pair_id"]] = c
            added += 1
    return existing, added


def run_match(cfg, out_path: str | Path = CANDIDATES_PATH) -> dict:
    deadline = Deadline(cfg["nonsports"]["match_deadline_secs"])
    concepts = load_concepts()
    k_rows = _kalshi_side(cfg, concepts, deadline)
    p_rows = _pm_side(cfg, deadline)
    cands = block_candidates(cfg, k_rows, p_rows, concepts)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged, added = merge_candidates(out_path, cands)
    out_path.write_text(yaml.safe_dump(merged, sort_keys=True, width=100,
                                       allow_unicode=True))
    summary = {"kalshi_rows": len(k_rows), "pm_rows": len(p_rows),
               "candidates": len(cands), "new": added, "total_on_file": len(merged),
               "unverdicted": sum(1 for v in merged.values()
                                  if "verdict" not in v)}
    log.info("nonsports match: %s", summary)
    return summary
