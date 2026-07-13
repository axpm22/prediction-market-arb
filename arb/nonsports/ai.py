"""AI matcher stage (plan Phase 1.4 — rule 11: manual-first, Haiku-SECOND).

Classifies BLOCKED candidate pairs only (never raw markets), verdicts cached
append-only in the `verdicts` table under (pair_key, rules_sha, model), spend
capped by ai_matcher.max_new_pairs_per_day. Two modes:
  eval — classify pairs that ALREADY carry a human verdict; evaluate() then
         scores precision against that ground truth (§18.5). Runs while
         ai_matcher.enabled is false: it is the evidence for enabling.
  live — classify unverdicted pairs; requires ai_matcher.enabled (flipped only
         by dated amendment after the pre-registered precision bar passes).
AI verdicts NEVER promote a pair to nonsports_map.yaml — downstream stays
REVIEW/ai_matched (classify(), rule 11); a settlement mismatch permanently
bans the pair. Never used for market data (hard rule 1 scope note).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

from arb.logutil import get_logger
from arb.nonsports.scan import CANDIDATES_PATH

log = get_logger("arb.nonsports.ai", "logs/scan.log")

# Haiku 4.5 per-1M-token prices (skill table, cached 2026-06); cost is a
# recorded fact per verdict, not a gate
_IN_PER_TOK = Decimal("1.00") / Decimal(10 ** 6)
_OUT_PER_TOK = Decimal("5.00") / Decimal(10 ** 6)

SCHEMA = {
    "type": "object",
    "properties": {
        "same_event": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["same_event", "confidence", "reason"],
    "additionalProperties": False,
}

PROMPT = """You judge whether two prediction markets are SETTLEMENT-IDENTICAL:
would every possible state of the world settle both the same way? Same topic
is NOT enough. Check, in order: (1) resolution source; (2) threshold and
direction — '>= 3.0%' vs '> 3.0%' differ, 'more than X' (threshold) vs
'exactly X' (bucket) differ; (3) units, rounding, seasonal adjustment,
headline vs core; (4) measurement period AND settlement cutoff; (5) entity
precision — 'a Republican' is not 'Trump', China is not South Korea; (6)
inverted framing can still be identical (note it in reason); (7) divergent
cancellation/revision handling means different. When uncertain, same_event is
false and confidence is low. Confidence is your probability the verdict is
correct, 0-1."""

_RULES_SHA = hashlib.sha1(PROMPT.encode()).hexdigest()


def _client():
    try:
        import anthropic
    except ImportError as e:  # optional extra, never in requirements.txt core
        raise RuntimeError(
            "the AI matcher needs the optional anthropic package: "
            ".venv/bin/pip install anthropic") from e
    return anthropic.Anthropic()  # ANTHROPIC_API_KEY from the environment/.env


def classify_pair(client, model: str, cand: dict, max_chars: int) -> dict:
    half = max_chars // 2
    user = (f"MARKET A (kalshi): {cand.get('kalshi_text', '')}\n"
            f"A rules: {cand.get('kalshi_rules', '')[:half]}\n\n"
            f"MARKET B (polymarket): {cand.get('pm_text', '')}\n"
            f"B rules: {cand.get('pm_rules', '')[:half]}")
    resp = client.messages.create(
        model=model, max_tokens=1024, system=PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    data = json.loads(resp.content[0].text)
    cost = (Decimal(resp.usage.input_tokens) * _IN_PER_TOK
            + Decimal(resp.usage.output_tokens) * _OUT_PER_TOK)
    return {"same_event": bool(data["same_event"]),
            "confidence": Decimal(str(data["confidence"])),
            "reason": str(data["reason"])[:500], "cost_usd": cost}


def _cached(store, pair_key: str, model: str) -> bool:
    return bool(store.query(
        "SELECT 1 FROM verdicts WHERE pair_key=? AND rules_sha=? AND model=? "
        "LIMIT 1", (pair_key, _RULES_SHA, model)))


def _spent_today(store, now: datetime) -> int:
    return store.query("SELECT COUNT(*) c FROM verdicts WHERE created_at >= ?",
                       (now.date().isoformat(),))[0]["c"]


def run_ai(cfg, store, mode: str = "eval", client=None,
           candidates_path: str | Path = CANDIDATES_PATH,
           now: datetime | None = None) -> dict:
    assert mode in ("eval", "live", "sweep")
    now = now or datetime.now(timezone.utc)
    aicfg = cfg["ai_matcher"]
    if mode == "live" and not aicfg["enabled"]:
        return {"status": "disabled", "classified": 0,
                "note": "ai_matcher.enabled is false — the §18.5 precision bar "
                        "and a dated amendment flip it, nothing else"}
    path = Path(candidates_path)
    cands = yaml.safe_load(path.read_text()) or {}
    # eval scores curated pairs; live extends to the unverdicted frontier;
    # sweep classifies everything uncached (second-opinion / pre-compute) —
    # verdicts are cached either way, so no call is ever repeated
    todo = [c for c in cands.values()
            if (("verdict" in c) if mode == "eval" else
                ("verdict" not in c) if mode == "live" else True)
            and not _cached(store, c["pair_id"], str(aicfg["model"]))]
    budget = int(aicfg["max_new_pairs_per_day"]) - _spent_today(store, now)
    classified, budget_exhausted = 0, False
    if client is None and todo and budget > 0:
        client = _client()
    errors = 0
    for c in todo:
        if classified >= budget:
            budget_exhausted = True
            break
        try:
            v = classify_pair(client, str(aicfg["model"]), c,
                              int(aicfg["max_rules_chars"]))
        except Exception as e:  # noqa: BLE001 — one bad pair never kills a run
            log.warning("ai classify failed for %s: %s", c["pair_id"], e)
            errors += 1
            if errors >= 5:
                log.warning("ai matcher: %s consecutive-ish errors — stopping "
                            "the run early", errors)
                break
            continue
        store.insert("verdicts", {
            "created_at": now.isoformat(), "pair_key": c["pair_id"],
            "verdict": "same" if v["same_event"] else "different",
            "confidence": v["confidence"], "rules_sha": _RULES_SHA,
            "model": str(aicfg["model"]), "cost_usd": v["cost_usd"]})
        # convenience annotation for the curator; never touches human fields
        c.setdefault("ai_verdict", "same" if v["same_event"] else "different")
        c.setdefault("ai_confidence", float(v["confidence"]))
        c.setdefault("ai_reason", v["reason"])
        classified += 1
    if classified:
        path.write_text(yaml.safe_dump(cands, sort_keys=True, width=100,
                                       allow_unicode=True))
    s = {"status": "ok", "mode": mode, "classified": classified,
         "budget_exhausted": budget_exhausted, "errors": errors,
         "cached_or_done": len(cands)}
    log.info("ai matcher: %s", s)
    return s


def evaluate(cfg, store, candidates_path: str | Path = CANDIDATES_PATH) -> dict:
    """Precision of AI 'same' at >= min_confidence against human verdicts —
    the §18.5 number the pre-registered bar judges."""
    aicfg = cfg["ai_matcher"]
    cands = yaml.safe_load(Path(candidates_path).read_text()) or {}
    human = {k: v["verdict"] for k, v in cands.items()
             if v.get("verdict") in ("same", "different")}
    tp = fp = fn = tn = 0
    n = 0
    for row in store.query(
            "SELECT pair_key, verdict, confidence FROM verdicts "
            "WHERE rules_sha=? AND model=?", (_RULES_SHA, str(aicfg["model"]))):
        truth = human.get(row["pair_key"])
        if truth is None:
            continue
        n += 1
        says_same = (row["verdict"] == "same"
                     and Decimal(str(row["confidence"]))
                     >= Decimal(str(aicfg["min_confidence"])))
        if says_same and truth == "same":
            tp += 1
        elif says_same and truth == "different":
            fp += 1
        elif not says_same and truth == "same":
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    return {"n_scored": n, "true_positives": tp, "false_positives": fp,
            "false_negatives": fn, "true_negatives": tn,
            "precision": precision,
            "min_confidence": float(aicfg["min_confidence"])}
