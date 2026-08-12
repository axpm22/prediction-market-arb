"""Haiku-decided non-sports market matching (spec amendment to "manual-only" — see
docs/nonsports_ai_matcher.md). Haiku compares both venues' resolution rules and decides
equivalence inline in the nonsports watch loop; every AI-matched event is tagged
ai_matched=true, its opportunities stay REVIEW, and its bets report in a separate section
so the tracker measures Haiku's real error rate instead of trusting it.

Cost guards: append-only verdict cache (never re-ask a decided pair) + per-day new-pair
budget counted in the DB. Pair identity = (kalshi series, polymarket event) — the stable
discovery-level ids; a series/event with more than one open market is never AI-matched
(ambiguous mapping stays manual). SX is excluded: no rules text.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .store import Store

SYSTEM_PROMPT = """You compare resolution rules of two prediction-market contracts and decide if
they settle identically. You are pessimistic: any difference in metric,
threshold, deadline, data source, or edge-case handling (postponement, ties,
withdrawal, revisions, which data release) means NOT equivalent.

Extract for each contract:
- entity: who/what the contract is about
- metric: exact quantity or outcome measured, with comparator and threshold
- deadline: resolution date/time and timezone
- source: the authority or data release that decides settlement
- edge_cases: any special voiding/tie/revision rules

STRIKE CONTRACTS (weather bands, macro thresholds — temperature, CPI, Fed target
range, jobless claims): a deterministic pre-filter has already confirmed the two
strikes look numerically aligned. Your job is the rules/source equivalence the
numbers can't show. Extract and compare precisely:
- comparator + threshold + unit (>=, >, <=, <; °F vs °C; %, bps, index points, count)
- measurement window: the exact local date AND timezone
- rounding rule (e.g. nearest whole degree vs one decimal)
- the EXACT data source: station, index, and release — "NWS Climatological Report,
  Central Park (KNYC)" is NOT the same as "Weather Underground KNYC" or a different
  station. Different station, different rounding, or different release ⇒ equivalent:false
  no matter how aligned the strikes look.

Then answer with ONLY this JSON, no other text:
{
  "a": {"entity": "", "metric": "", "deadline": "", "source": "", "edge_cases": ""},
  "b": {"entity": "", "metric": "", "deadline": "", "source": "", "edge_cases": ""},
  "comparator_a": "", "comparator_b": "",
  "threshold_a": "", "threshold_b": "",
  "unit_a": "", "unit_b": "",
  "data_source_a": "", "data_source_b": "",
  "rounding_a": "", "rounding_b": "",
  "equivalent": true|false,
  "confidence": 0.0-1.0,
  "differences": ["each material difference, one short line", ...]
}

equivalent=true requires: same entity, same metric+threshold, same deadline
(±1 day only if both sources publish once), same or officially-identical
source, and no conflicting edge-case rules. For strike contracts additionally:
identical comparator+threshold+unit, identical measurement window and timezone,
identical rounding, and the SAME data source/station. When in doubt, false."""

USER_TEMPLATE = """CONTRACT A ({venue_a}: {id_a})
Title: {title_a}
Rules:
{rules_a}

CONTRACT B ({venue_b}: {id_b})
Title: {title_b}
Rules:
{rules_b}"""

API_TIMEOUT_SECS = 10.0
MAX_API_ATTEMPTS = 3


class MatcherUnavailable(RuntimeError):
    """Billing/auth/bad-request failure — retrying can't heal it, so it must not burn
    per-pair attempts or record verdicts; the stage stops asking until it's fixed."""
# ponytail: API-failure attempts are counted in-memory, so a process restart re-tries a
# failed pair a few extra times. Bounded by the daily budget; move to a table if it matters.
_attempts: dict[str, int] = {}


def make_client(cfg: dict[str, Any]):
    import anthropic  # lazy: only needed when ai_matcher.enabled

    return anthropic.Anthropic(timeout=API_TIMEOUT_SECS, max_retries=0)


def pair_key(venue_a: str, id_a: str, venue_b: str, id_b: str) -> str:
    return "|".join(sorted([f"{venue_a}:{id_a}", f"{venue_b}:{id_b}"]))


def rules_sha(text: str | None) -> str:
    """Phase E: the verdict is about the rules texts, so hash them (normalized whitespace) —
    a venue edit to rules_primary/description changes this and expires the cache."""
    import hashlib
    normalized = " ".join(str(text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def cached_verdict(store: Store, key: str):
    # Append-only table can hold several rows per pair (re-verdicts, bans) — the LATEST wins.
    with store.connect() as con:
        return con.execute(
            "SELECT * FROM nonsports_verdicts WHERE pair_key = ? ORDER BY id DESC LIMIT 1",
            (key,)).fetchone()


def verdict_is_current(row, rules_a: str | None, rules_b: str | None) -> bool:
    """Phase E: a cached verdict is only valid if it was made against the CURRENT rules texts.
    Order-insensitive (pair_key sorts the sides). Missing stored hashes (pre-Phase-E rows) are
    treated as stale so they get re-verdicted once."""
    stored = {row["rules_hash_a"], row["rules_hash_b"]} if "rules_hash_a" in row.keys() else {None}
    if None in stored:
        return False
    return stored == {rules_sha(rules_a), rules_sha(rules_b)}


def calls_today(store: Store, now: datetime) -> int:
    with store.connect() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM nonsports_verdicts WHERE substr(checked_at, 1, 10) = ?",
            (now.date().isoformat(),),
        ).fetchone()
    return int(row[0])


def save_verdict(store: Store, key: str, a: dict[str, str], b: dict[str, str],
                 verdict: dict[str, Any], model: str, now: datetime) -> None:
    extracted_a = verdict.get("a") or {}
    with store.connect() as con:
        con.execute(
            # pair_key is UNIQUE (operational cache, one row per pair): a re-verdict — including a
            # Phase E rules-change expiry — REPLACES the stale row rather than colliding.
            """INSERT OR REPLACE INTO nonsports_verdicts(checked_at, pair_key, venue_a, market_a, venue_b,
                   market_b, equivalent, confidence, differences_json, entity, metric, deadline,
                   source_a, source_b, model, raw_response,
                   comparator_a, comparator_b, threshold_a, threshold_b, unit_a, unit_b,
                   data_source_a, data_source_b, rounding_a, rounding_b, rules_hash_a, rules_hash_b)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now.isoformat(), key, a["venue"], a["id"], b["venue"], b["id"],
                None if verdict.get("equivalent") is None else int(bool(verdict["equivalent"])),
                str(verdict.get("confidence") or 0),
                json.dumps(verdict.get("differences") or []),
                extracted_a.get("entity"), extracted_a.get("metric"), extracted_a.get("deadline"),
                extracted_a.get("source"), (verdict.get("b") or {}).get("source"),
                model, str(verdict.get("raw", ""))[:4000],
                verdict.get("comparator_a"), verdict.get("comparator_b"),
                verdict.get("threshold_a"), verdict.get("threshold_b"),
                verdict.get("unit_a"), verdict.get("unit_b"),
                verdict.get("data_source_a"), verdict.get("data_source_b"),
                verdict.get("rounding_a"), verdict.get("rounding_b"),
                rules_sha(a.get("rules")), rules_sha(b.get("rules")),
            ),
        )


def classify_pair(client, model: str, a: dict[str, str], b: dict[str, str],
                  max_rules_chars: int) -> dict[str, Any]:
    """One Haiku equivalence verdict. Invalid JSON is retried once, then recorded as
    equivalent=null. API errors propagate to the caller (retried across polls)."""
    message = USER_TEMPLATE.format(
        venue_a=a["venue"], id_a=a["id"], title_a=a["title"], rules_a=_clip(a["rules"], max_rules_chars),
        venue_b=b["venue"], id_b=b["id"], title_b=b["title"], rules_b=_clip(b["rules"], max_rules_chars),
    )
    raw = ""
    for _ in range(2):  # spec: retry once on non-JSON output
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": message}],
        )
        raw = next((block.text for block in response.content if block.type == "text"), "")
        verdict = _parse_verdict(raw)
        if verdict is not None:
            verdict["raw"] = raw
            return verdict
    return {"equivalent": None, "confidence": 0, "raw": raw}


def resolve_verdict(store: Store, cfg: dict[str, Any], a: dict[str, str], b: dict[str, str],
                    now: datetime, client_factory: Callable = make_client):
    """Return the verdict row for a pair, asking Haiku iff there is no cached verdict and
    the daily budget allows. Returns None when undecided (over budget / API error pending)."""
    key = pair_key(a["venue"], a["id"], b["venue"], b["id"])
    row = cached_verdict(store, key)
    if row is not None and verdict_is_current(row, a.get("rules"), b.get("rules")):
        return row
    if row is not None:
        # Phase E: rules text changed since the cached verdict. Fail-safe: a previously-EQUIVALENT
        # pair drops to unmatched immediately and its open paper bets are annotated
        # `rules-changed-midflight` (the purest settlement-risk specimen). A re-verdict is queued
        # below (budget permitting); negative verdicts re-check too — a clarified rule can create
        # equivalence (free inventory).
        if row["equivalent"] == 1:
            annotate_rules_changed(store, key, now)
    if calls_today(store, now) >= int(cfg["max_new_pairs_per_day"]):
        print(f"ai-matcher: daily pair budget reached; skipping {key}")
        return None
    try:
        verdict = classify_pair(client_factory(cfg), str(cfg["model"]), a, b,
                                int(cfg["max_rules_chars"]))
    except Exception as exc:  # noqa: BLE001 — API/timeout errors: record nothing, retry next poll
        if getattr(exc, "status_code", None) in (400, 401, 403):
            raise MatcherUnavailable(str(exc)) from exc
        _attempts[key] = _attempts.get(key, 0) + 1
        if _attempts[key] < MAX_API_ATTEMPTS:
            print(f"ai-matcher: attempt {_attempts[key]} failed for {key}: {exc}")
            return None
        verdict = {"equivalent": None, "confidence": 0, "raw": f"gave up after {MAX_API_ATTEMPTS} API errors: {exc}"}
    save_verdict(store, key, a, b, verdict, str(cfg["model"]), now)
    return cached_verdict(store, key)


def annotate_rules_changed(store: Store, key: str, now: datetime) -> int:
    """Phase E fail-safe: annotate every OPEN nonsports paper bet on this pair
    `rules-changed-midflight` when the pair's rules text changed under it. Append-only; Phase C
    markout analysis excludes these and settlement watches them specially. Returns rows annotated."""
    from arb.experimental.annotations import annotate
    ids = {part.split(":", 1)[1] for part in key.split("|") if ":" in part}
    annotated = 0
    with store.connect() as con:
        rows = list(con.execute(
            "SELECT id, legs_json FROM flat_bets WHERE status='OPEN' AND category='nonsports'"))
    for row in rows:
        legs = json.loads(row["legs_json"] or "[]")
        if any(str(leg.get("book")) in ids or str(leg.get("ref")) in ids for leg in legs):
            try:
                annotate(store, int(row["id"]), "rules-changed-midflight",
                         f"rules text changed for AI pair {key} at {now.isoformat()}")
                annotated += 1
            except ValueError:
                pass
    return annotated


def ban_pair(store: Store, key: str, reason: str, now: datetime, model: str = "settlement-mismatch") -> None:
    """Force a pair's cached verdict to NEGATIVE so it is never paper-bet again (the
    settlement-mismatch writeback — the fake-arb the rules feared actually fired). Upserts on
    pair_key: this is a ground-truth override, so it may overwrite a prior positive verdict
    (the one exception to the append-only cache — banning a proven-bad pair wins)."""
    with store.connect() as con:
        con.execute(
            """INSERT INTO nonsports_verdicts(checked_at, pair_key, venue_a, market_a, venue_b,
                   market_b, equivalent, confidence, differences_json, model, raw_response)
               VALUES (?, ?, '', '', '', '', 0, '1', ?, ?, ?)
               ON CONFLICT(pair_key) DO UPDATE SET
                   equivalent=0, confidence='1', differences_json=excluded.differences_json,
                   model=excluded.model, checked_at=excluded.checked_at""",
            (now.isoformat(), key, json.dumps([reason]), model, reason),
        )


def verdict_matches(row, min_confidence: float) -> bool:
    from decimal import Decimal, InvalidOperation

    if row is None or row["equivalent"] != 1:
        return False
    try:
        return Decimal(str(row["confidence"])) >= Decimal(str(min_confidence))
    except InvalidOperation:
        return False


def _clip(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[rules truncated for length]"


def _parse_verdict(raw: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        verdict = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(verdict, dict) or not isinstance(verdict.get("equivalent"), bool):
        return None
    return verdict
