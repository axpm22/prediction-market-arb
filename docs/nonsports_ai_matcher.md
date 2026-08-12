# Task: Haiku-decided non-sports market matching (spec amendment)

Read `CLAUDE.md` and `SPEC.md` first. **This task amends the "non-sports matching is manual-only" rule** (CLAUDE.md workflow + SPEC §1/§5.4): Haiku now decides equivalence automatically, inline in the watch loop, because opportunities close before a human can approve. Update CLAUDE.md and SPEC.md to reflect the amendment as part of this task. All other hard rules stand — especially: paper only, Decimal money math, append-only ledgers.

Safety valve replacing human approval: every AI-matched event is tagged `ai_matched=true`, its opportunities are always `status=REVIEW`, and its tracker bets appear in a **separate "AI-matched" report section — never in the headline number**. The tracker thus measures Haiku's real error rate (AI-matched flagged edge vs realized edge) instead of trusting it.

## What to build

### 1. `arb/nonsports_ai.py` (keep under ~300 lines)

Called from the matching stage of the watch loop whenever a non-sports fuzzy candidate pair (title similarity ≥ 85, per SPEC §5.4) has **no cached verdict**:

1. Fetch full resolution rules text:
   - Kalshi: `GET /markets/{ticker}` → `rules_primary` (+ `rules_secondary` if present). Public GET.
   - Polymarket: Gamma `description` field (already in the discovery payload — reuse it, don't re-fetch).
   - SX Bet: excluded — no rules text; SX non-sports pairs are never AI-matched.
2. Send both rules texts to the Anthropic API (config model, default `claude-haiku-4-5`) with the classifier prompt in §3. `max_tokens: 1024`, `temperature: 0`, `anthropic` package pinned in `requirements.txt`, `ANTHROPIC_API_KEY` from `.env` (add to `.env.example`).
3. Persist the verdict, append-only:
   `nonsports_verdicts(id, checked_at, venue_a, market_a, venue_b, market_b, equivalent, confidence, differences_json, entity, metric, deadline, source_a, source_b, model, raw_response)`
4. **Decision:** `equivalent=true AND confidence ≥ ai_matcher.min_confidence` → the pair is matched: create/extend the event with `ai_matched=true` and let it flow into detection this same poll. Anything else → not matched; still logged to `logs/nonsports_candidates.log` as before.
5. **Verdict cache:** key = sorted (venue, market-id) pair. Never re-ask Haiku for a pair that has a verdict (any verdict). This bounds cost and keeps the watch loop fast — at most `max_new_pairs_per_day` Haiku calls per day (config), counted in the DB; over budget → skip and log.

Latency rule: Haiku calls must not block quote collection. Collect quotes first, then run pending verdict lookups; a pair matched this poll is eligible from this poll onward. On API error/timeout (10 s), record nothing and retry next poll (max 3 attempts per pair, then give up permanently with `equivalent=null`).

### 2. Model / tracker changes

- `Event` gets `ai_matched: bool` (default false). Flows into `opportunities.notes` and a new `flat_bets.ai_matched` column.
- Opportunities on AI-matched events: always `status=REVIEW`, note `ai-matched conf=0.93`.
- Daily report: AI-matched bets get their own section with the same per-class breakdown, plus **flagged edge vs realized edge for AI-matched pairs only** — this line is the Haiku error-rate monitor. Headline number unchanged (trusted + sports + pregame only).
- `nonsports_map.yaml` remains: human entries there are matched exactly as before and are NOT `ai_matched`. Human file always wins over an AI verdict if both exist for a pair.

### 3. Config (`config.yaml`)

```yaml
ai_matcher:
  enabled: false            # off by default; turn on manually
  model: claude-haiku-4-5
  min_confidence: 0.85
  max_new_pairs_per_day: 100   # cost guard
  max_rules_chars: 8000        # truncate each side; note truncation in the request
```

Missing key or `enabled: false` → matching silently falls back to manual-only behavior (log one line per run, don't crash the loop).

### 4. The Haiku classifier prompt

System prompt (module-level constant):

```
You compare resolution rules of two prediction-market contracts and decide if
they settle identically. You are pessimistic: any difference in metric,
threshold, deadline, data source, or edge-case handling (postponement, ties,
withdrawal, revisions, which data release) means NOT equivalent.

Extract for each contract:
- entity: who/what the contract is about
- metric: exact quantity or outcome measured, with comparator and threshold
- deadline: resolution date/time and timezone
- source: the authority or data release that decides settlement
- edge_cases: any special voiding/tie/revision rules

Then answer with ONLY this JSON, no other text:
{
  "a": {"entity": "", "metric": "", "deadline": "", "source": "", "edge_cases": ""},
  "b": {"entity": "", "metric": "", "deadline": "", "source": "", "edge_cases": ""},
  "equivalent": true|false,
  "confidence": 0.0-1.0,
  "differences": ["each material difference, one short line", ...]
}

equivalent=true requires: same entity, same metric+threshold, same deadline
(±1 day only if both sources publish once), same or officially-identical
source, and no conflicting edge-case rules. When in doubt, false.
```

User message per pair:

```
CONTRACT A ({venue_a}: {ticker_or_slug_a})
Title: {title_a}
Rules:
{rules_a}

CONTRACT B ({venue_b}: {ticker_or_slug_b})
Title: {title_b}
Rules:
{rules_b}
```

If the response is not valid JSON after stripping code fences, retry once; then record `equivalent=null, confidence=0` with the raw text.

## Hard constraints

- **No live Anthropic or venue calls in tests.** Fixtures: one real Kalshi `rules_primary`, one real Polymarket description, canned Haiku verdicts (true-high-conf, true-low-conf, false, garbage). Tests assert: high-conf true → event created with `ai_matched=true` and REVIEW opportunities; low-conf true → not matched; garbage → `equivalent=null` after retry; cached pair → zero API calls on second poll.
- **Verdicts table and `flat_bets` stay append-only.**
- AI-matched bets never enter the headline daily number.
- Cost guards enforced in code, not by hope: per-day pair budget + verdict cache.

## Acceptance

1. `pytest` green including the new tests.
2. Simulated 2-poll fixture run: poll 1 sees a new candidate pair → mocked verdict true/0.93 → poll 1 detection includes the AI-matched event, REVIEW status; poll 2 makes zero matcher API calls (cache hit). Show the report section with the AI-matched breakdown.
3. With a real key and `enabled: true`, one live `watch --once` where at least one non-sports pair exists shows the verdict landing in `nonsports_verdicts` and the pair either matched or logged.

---

## Amendment — strike-aware matching (STRIKE_AWARE_NONSPORTS_MATCHER_PROMPT.md, shipped 2026-07-02)

Multi-strike weather/macro series (Kalshi lists ~6 temp bands/day; CPI, Fed target range,
jobless claims) previously skipped because the one-shot path required exactly one open
market per venue. Now a two-stage pipeline handles them:

- **Stage 1 (free, deterministic — `arb/experimental/strike_match.py`):** parse each market
  into a normalized `StrikeSpec` (Kalshi's structured `strike_type`/`cap_strike`/`floor_strike`
  first, then title/question text), block on same close date + fuzzy entity ≥ 85, and pair
  only strikes that align EXACTLY (identical comparator+value+unit, or identical range
  bounds+inclusivity). No integer-domain reasoning, no half-width tolerance — `>99` vs `>=100`
  and `[70,75)` vs `[70,72.5)` never pair. °F↔°C conversion only when a side states both.
- **Stage 2 (Haiku):** the system prompt gains a STRIKE section — extract & compare
  comparator+threshold+unit, measurement window + timezone, rounding, and the EXACT data
  source/station. Verdict JSON gains `comparator_a/b`, `threshold_a/b`, `unit_a/b`,
  `data_source_a/b`, `rounding_a/b` (stored on `nonsports_verdicts`). Different station or
  rounding ⇒ `equivalent:false`.
- **Cache** is keyed at market level (`kalshi:<market_ticker>|polymarket:<condition_id>`);
  legacy series-level keys still hit for single-market pairs. Negative verdicts short-circuit
  forever. Daily weather strikes never hit across days (per-day budget is the control).
- **Paper lane** (`ai_matcher.paper_fill`, `paper_stake_usd`): AI-matched flags are booked $1
  flat in a segregated slice (REVIEW, untrusted, off the headline). A settled pair whose two
  venues disagree is flagged `settlement_mismatch`, banned via a negative verdict, never re-bet
  — this mismatch counter is the lane's most important output.
