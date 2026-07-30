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
from arb.nonsports.match import SHAPE_VERSION, shape_relation
from arb.nonsports.scan import CANDIDATES_PATH

log = get_logger("arb.nonsports.ai", "logs/scan.log")

# Haiku 4.5 per-1M-token prices (skill table, cached 2026-06); cost is a
# recorded fact per verdict, not a gate
_IN_PER_TOK = Decimal("1.00") / Decimal(10 ** 6)
_OUT_PER_TOK = Decimal("5.00") / Decimal(10 ** 6)

SCHEMA = {
    "type": "object",
    "properties": {
        # v3: the AI answers ONLY source/measurand/period identity. Boundary
        # and threshold-shape logic is decided in code (match.shape_relation)
        # — v2 showed Haiku fails boundary worlds at 0.95 confidence while
        # nailing source identification in every reason text (2026-07-13).
        "same_measurand": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["same_measurand", "confidence", "reason"],
    "additionalProperties": False,
}

PROMPT = """Two prediction markets each settle on a published statistic. \
Ignore the thresholds, directions, and strike values completely — another \
system checks those. Answer ONE question: do both markets settle on the SAME \
underlying published number?

same_measurand is true only if ALL of these match:
1. SOURCE: the same publication by the same institution. "reported by the \
BLS", "the Employment Situation report", and "the official unemployment rate \
(U-3)" all name the same print.
2. QUANTITY: exactly the same series. Headline CPI is not core CPI; \
seasonally adjusted is not unadjusted; MoM is not YoY; a LEVEL ("core CPI is \
X") is not a COMPARISON ("core above headline"); real GDP Advance Estimate \
is not a later revision.
3. ENTITY: same country/person/place. China is not South Korea; 'a \
Republican' is not 'Trump'.
4. PERIOD AND SETTLING RELEASE: same measurement period, settled by the same \
release. One side counting 2025 when the other starts at 2026 -> false.
5. EDGE CLAUSES: materially divergent cancellation/revision handling -> false.

When genuinely uncertain, same_measurand is false with low confidence. \
Confidence is your probability the answer is correct, 0-1."""

_RULES_SHA = hashlib.sha1((PROMPT + "|" + SHAPE_VERSION).encode()).hexdigest()


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
    return {"same_measurand": bool(data["same_measurand"]),
            "confidence": Decimal(str(data["confidence"])),
            "reason": str(data["reason"])[:500], "cost_usd": cost}


def system_verdict(shape: str, ai: dict | None) -> tuple[str, Decimal, str]:
    """(verdict, confidence, reason): code decides shape, AI decides measurand.

    A PARSED shape mismatch is decided in code and never reaches the API.
    'unknown' means no threshold regex fired, i.e. the code has NO opinion —
    which is precisely where the model's judgment is the point. Until
    2026-07-30 it short-circuited to ('different', 1.0) with no call and cached
    that, so the 'pending a human look' this docstring used to promise never
    arrived: _cached_keys drops the pair from every later run. That silently
    discarded 516 of 573 live candidates (90%).

    It still never yields 'same' on its own: an unparsed proposition has no
    verified boundary, and boundary worlds are exactly where the model failed
    at 0.95 confidence (first eval 2026-07-13). Same-measurand + unparsed shape
    is 'needs-human' — a curation queue, not a match."""
    if shape == "different":
        return "different", Decimal(1), "shape: boundary/threshold mismatch (code)"
    if ai is None:  # budget ran out before the call — stay conservative
        return "different", Decimal(1), "shape: unparsed, no model verdict (conservative)"
    if not ai["same_measurand"]:
        return "different", ai["confidence"], f"measurand: {ai['reason']}"
    if shape == "unknown":
        return ("needs-human", ai["confidence"],
                f"shape UNPARSED + measurand: {ai['reason']}")
    verdict = "same" if shape == "identical" else "same-inverted"
    return verdict, ai["confidence"], f"shape:{shape} + measurand: {ai['reason']}"


def _cached_keys(store, model: str) -> set:
    # one SELECT for the whole run, not one per candidate (13.8 GB live DB)
    return {r["pair_key"] for r in store.query(
        "SELECT pair_key FROM verdicts WHERE rules_sha=? AND model=?",
        (_RULES_SHA, model))}


def _spent_today(store, now: datetime) -> int:
    # shape verdicts are free and unlimited; the cap is Anthropic spend
    return store.query("SELECT COUNT(*) c FROM verdicts WHERE created_at >= ? "
                       "AND CAST(cost_usd AS REAL) > 0",
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
    cached = _cached_keys(store, str(aicfg["model"]))
    todo = [c for c in cands.values()
            if (("verdict" in c) if mode == "eval" else
                ("verdict" not in c) if mode == "live" else True)
            and c["pair_id"] not in cached]
    budget = int(aicfg["max_new_pairs_per_day"]) - _spent_today(store, now)
    classified, api_calls, budget_exhausted = 0, 0, False
    if client is None and todo and budget > 0:
        client = _client()
    errors = 0
    for c in todo:
        shape = shape_relation(
            f"{c.get('kalshi_text', '')} {c.get('kalshi_rules', '')}",
            c.get("pm_text", ""))
        ai = None
        # 'different' is the only shape code can settle alone; 'unknown' is the
        # code ABSTAINING and must be asked, not assumed (see system_verdict)
        if shape in ("identical", "inverted", "unknown"):
            if api_calls >= budget:
                # budget gates API spend only; shape-decided pairs below
                # keep classifying for free
                budget_exhausted = True
                continue
            api_calls += 1  # count the attempt: the spend cap is conservative
            try:
                ai = classify_pair(client, str(aicfg["model"]), c,
                                   int(aicfg["max_rules_chars"]))
            except Exception as e:  # noqa: BLE001 — one bad pair never kills a run
                log.warning("ai classify failed for %s: %s", c["pair_id"], e)
                errors += 1
                if errors >= 5:
                    log.warning("ai matcher: %s consecutive-ish errors — "
                                "stopping the run early", errors)
                    break
                continue
        verdict, conf, reason = system_verdict(shape, ai)
        store.insert("verdicts", {
            "created_at": now.isoformat(), "pair_key": c["pair_id"],
            "verdict": verdict, "confidence": conf, "rules_sha": _RULES_SHA,
            "model": str(aicfg["model"]),
            "cost_usd": ai["cost_usd"] if ai else Decimal(0)})
        # convenience annotation for the curator; never touches human fields
        c["ai_verdict"] = verdict
        c["ai_confidence"] = float(conf)
        c["ai_reason"] = reason[:500]
        classified += 1
    if classified:
        path.write_text(yaml.safe_dump(cands, sort_keys=True, width=100,
                                       allow_unicode=True))
    s = {"status": "ok", "mode": mode, "classified": classified,
         "api_calls": api_calls, "budget_exhausted": budget_exhausted,
         "errors": errors, "cached_or_done": len(cands)}
    log.info("ai matcher: %s", s)
    return s


def evaluate(cfg, store, candidates_path: str | Path = CANDIDATES_PATH) -> dict:
    """Precision of AI 'same' at >= min_confidence against human verdicts —
    the §18.5 number the pre-registered bar judges."""
    aicfg = cfg["ai_matcher"]
    cands = yaml.safe_load(Path(candidates_path).read_text()) or {}
    # any human same-* verdict (same, same-inverted) is truth 'same' — the AI
    # side already normalizes via startswith('same'); dropping same-inverted
    # silently shrank the §18.5 denominator
    human = {k: ("same" if v["verdict"].startswith("same") else "different")
             for k, v in cands.items()
             if isinstance(v.get("verdict"), str)
             and (v["verdict"].startswith("same") or v["verdict"] == "different")}
    tp = fp = fn = tn = 0
    n = 0
    for row in store.query(
            "SELECT pair_key, verdict, confidence FROM verdicts "
            "WHERE rules_sha=? AND model=?", (_RULES_SHA, str(aicfg["model"]))):
        truth = human.get(row["pair_key"])
        if truth is None:
            continue
        n += 1
        says_same = (row["verdict"].startswith("same")
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
