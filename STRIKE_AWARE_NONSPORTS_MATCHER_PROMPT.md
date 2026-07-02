# FEATURE: Strike-aware nonsports matching — weather & macro multi-strike series

Read `SPEC.md`, `CLAUDE.md`, and `prompts/nonsports_ai_matcher.md` first. All hard rules apply,
especially rule 11 (manual-first, Haiku-second; AI-matched = always REVIEW, tagged `ai_matched`,
own report section, never the headline; human `data/nonsports_map.yaml` entries always win).

## Context — why weather/macro currently produce nothing

The AI matcher (live since 2026-07-02, 8 verdicts in `nonsports_verdicts`) only evaluates pairs
where each venue has EXACTLY ONE open market per series/event
(`_kalshi_single_market` / `_polymarket_single_market_tokens` in `arb/experimental/nonsports.py`).
One-shot political markets pass. Weather and macro never do:

```
ai-matcher: skip kalshi:HIGHNY|polymarket:653298 — needs exactly one open market on each venue
```

Kalshi `HIGHNY` lists ~6 temperature-band markets per day; the matching Polymarket NYC-temp
event has one market per bucket. Same shape for CPI, Fed target range, jobless claims. Every
such pair is skipped before Haiku sees it. This change replaces the single-market restriction
with a two-stage pipeline: a free deterministic narrowing script, then Haiku only on
strike-aligned survivors, with verdicts cached so nothing is ever asked twice.

## Design

### Stage 1 — deterministic narrowing (zero API cost beyond the market listings already fetched)

New module `arb/experimental/strike_match.py` (keep nonsports.py under ~300 lines):

1. **Expand** every multi-market Kalshi series and Polymarket event in the watched categories
   into individual markets. Do not drop a series for having >1 open market — that was the bug.
2. **Parse a StrikeSpec from each market** (title + subtitle + rules text):
   - `kind`: `threshold` (comparator + value) or `range` (low, high, bound inclusivity)
   - `comparator`: `>=`, `>`, `<=`, `<` (thresholds only)
   - `value(s)`: Decimal (CLAUDE.md rule 5)
   - `unit`: normalize before comparing — °F/°C, %, bps, index points, count. °F↔°C conversion
     is allowed only when one side explicitly states the other's unit; otherwise unit mismatch
     = no candidate.
   - Parse failures return `None` → market falls back to the existing single-market path if
     applicable, else it is skipped WITH a one-line log (these logs are the backlog of parser
     gaps; don't silently swallow).
3. **Block, then align.** Candidate pairs require ALL of:
   - same close/expiry date (after tz normalization);
   - fuzzy entity/metric similarity ≥ `nonsports.candidate_threshold` (85, rapidfuzz — already
     a dependency) on the normalized title with strike text stripped;
   - **exact strike alignment**: threshold vs threshold with identical comparator semantics and
     equal Decimal values after unit normalization, or range vs range with identical bounds and
     inclusivity. `>99` vs `>=100` is NOT aligned for continuous quantities (temperature) even
     though it's equivalent for integers — the parser does not do integer-domain reasoning;
     leave those to a human map entry. Comparators must match, not just numbers.
   - Buckets must partition identically to pair as a set; a stray half-width bucket on one venue
     pairs nothing (a `[70,75)` vs `[70,72.5)` mismatch is exactly how fake arbs happen).
4. Output: list of `(kalshi_market_ticker, polymarket_condition_id, StrikeSpec)` candidates.

Everything above is pure computation on listings the watcher already fetched. No new endpoints
(rule 3) — market details come from the same Kalshi/Polymarket public GETs nonsports.py
already uses.

### Stage 2 — Haiku equivalence verdict (the same strategy as political, extended)

Reuse `resolve_verdict` / the existing prompt file, with additions:

- The prompt (`prompts/nonsports_ai_matcher.md`) gains a strike section: Haiku must extract and
  compare, from each side's FULL resolution rules: comparator + threshold + unit, measurement
  window (local date + timezone!), rounding rules, and — critical for weather — the **exact data
  source** (e.g. "NWS Climatological Report for Central Park station" vs "Weather Underground
  KNYC"). Different station or different rounding ⇒ `equivalent: false` no matter how aligned
  the strikes look. This is the SAME_REQUIRED source rule from `data/nonsports_map.yaml`,
  enforced by the model instead of a human.
- Haiku's verdict schema gains fields: `comparator_a/b`, `threshold_a/b`, `unit_a/b`,
  `data_source_a/b`, `rounding_a/b`. Keep the existing `equivalent`, `confidence`,
  `differences` fields — the current 8 verdicts prove that shape works.
- Existing gates unchanged: `min_confidence` 0.85, `max_new_pairs_per_day` 100 (shared budget —
  strike pairs will consume it faster; that's fine, the cache makes day 2 cheap),
  `max_rules_chars` truncation with a note.
- A series-level Haiku sanity check is NOT needed — stage 1 already guarantees per-strike
  alignment; Haiku's job is rules/source equivalence only.

### Remember the result — verdict cache at market level

`nonsports_verdicts` already caches by `pair_key` append-only. Changes:

- **Key at market level**: `kalshi:<market_ticker>|polymarket:<condition_id>` (the old
  series-level keys like `kalshi:KXRATECUT|polymarket:79124` stay valid for single-market
  pairs — no migration, no row edits; the cache is append-only like everything else).
- Before any Haiku call, check the cache: a cached verdict (positive OR negative) at
  sufficient confidence is reused, zero API calls. This already exists (`cached_verdict`) —
  just make sure the market-level key hits it.
- Negative verdicts must also short-circuit stage 2 forever (they already do via
  `verdict_matches` — verify with a test).
- Deterministic stage-1 rejections are NOT cached — they're free to recompute and market
  listings churn daily.
- Daily strikes: weather markets are new every day (HIGHNY-26JUL03-B100 ≠ ...JUL04...). The
  cache will never hit across days for those — that is correct behavior, not a bug. The
  per-day budget is the cost control.

### Betting path — PAPER-fill AI-matched flags (rules amendment, owner-approved 2026-07-02)

This is and stays paper trading (CLAUDE.md rule 1 — no order placement, ever). The change:
AI-matched opportunities are no longer display-only; they are BOOKED in the flat-stake paper
tracker so we accumulate real P&L evidence on the AI matcher.

- New config under `ai_matcher`: `paper_fill: true`, `paper_stake_usd: 1` (flat, Decimal —
  no Kelly for this lane).
- When a strike-aligned pair has a cached positive verdict (≥ min_confidence) and prices
  cross after fees, append a `flat_bets` row exactly like sports flags: `status=REVIEW`,
  `trusted=false`, tagged `ai_matched`, `notes` carrying the verdict pair_key. Append-only
  (rule 9); net of fees and spread crossing.
- **Segregation (amends rule 11's "never auto-filled" for the PAPER ledger only):** ai_matched
  rows are their own population slice — never in the headline, never mixed into the clean
  lane (`quality_flag='REVIEW'` keeps them out per A1). Reports show them as a separate
  section with their own optimistic/confirmed × flat columns. Update CLAUDE.md rule 11 and
  the SPEC amendment text to say: displayed AND paper-tracked in a segregated lane; still
  never eligible for the trusted/whitelisted path without a human `nonsports_map.yaml` entry.
- **Settlement:** resolve each leg from its own venue's public resolution (existing Kalshi
  `result` / Polymarket `outcomePrices` snap resolvers). If the two venues disagree on the
  SAME ai-matched pair — that's the fake-arb case the rules feared — settle both legs
  honestly (one wins, one loses), let the P&L show the damage, and flag the row
  `settlement_mismatch` plus write the pair_key to a negative verdict so it is never
  paper-bet again. This mismatch counter is the single most important output of the lane.
- Fallback for markets a resolver can't read: existing
  `python -m arb settle --manual nonsports:<id> <YES|NO>`.

Real-money promotion path is unchanged and out of scope: human-reviewed `nonsports_map.yaml`
entry → trusted lane. The paper lane is the evidence factory for that review.

## Tasks

1. `arb/experimental/strike_match.py`: StrikeSpec parser + blocking + alignment. Pure
   functions, no I/O.
2. Wire into the nonsports watch loop, replacing the hard single-market skip: single-market
   pairs keep the existing path; multi-market pairs go through strike_match.
3. Extend `prompts/nonsports_ai_matcher.md` and the verdict parsing per above.
4. Fixtures (public GETs, free, recorded once): one day of Kalshi `HIGHNY` markets with full
   rules; the matching Polymarket temperature event; one macro multi-strike pair (Fed target
   range or CPI). Redact nothing — rules text is the test substance.
5. Tests (fixtures only, no network):
   - Parser vectors: `">=100°F"`, `"100° or above"`, `"between 96-97°"`, `[a,b)` ranges, `%`,
     bps, a °C/°F case, and at least 3 unparseable titles returning None.
   - Alignment: identical buckets pair; off-by-half-width buckets pair nothing; `>99` vs
     `>=100` rejected; comparator mismatch rejected; different close dates rejected.
   - Cache: first pass calls the (mocked) Haiku client N times; an identical second pass calls
     it ZERO times and yields identical flags. Negative verdicts also short-circuit.
   - Budget: pairs beyond `max_new_pairs_per_day` are deferred, not dropped-forever.
   - Rule 11 (amended): every AI-matched flag is REVIEW + `ai_matched` regardless of
     confidence; with `paper_fill: true` it books exactly one append-only flat_bets row
     (dedup by position_key — a persisting opportunity must not book twice).
   - Paper fill: crossed prices + positive cached verdict ⇒ row appended with Decimal stake,
     fees included; negative or missing verdict ⇒ no row. `paper_fill: false` ⇒ display-only
     (old behavior).
   - Settlement mismatch: fixture where Kalshi resolves YES and Polymarket NO for the same
     pair ⇒ both legs settled, row flagged `settlement_mismatch`, negative verdict appended,
     pair never re-bet.
6. Update CLAUDE.md rule 11 + the SPEC amendment wording per the betting-path section.
7. One live `nonsports` watch pass. Show: HIGHNY strike pairs reaching Haiku (aligned only),
   verdicts written, the previous "needs exactly one open market" spam gone, and any
   ai_matched paper bets appearing in their own report section (not the headline). Then run
   a second pass and show zero new Haiku calls and zero duplicate bets for unchanged pairs.

## Out of scope

- No REAL-money path of any kind (rule 1 is untouched); no trusted-lane/whitelist changes —
  ai_matched paper bets stay in their segregated slice.
- No °F↔°C inference beyond explicitly stated equivalents.
- No integer-domain comparator reasoning (`>99 ≡ >=100`); human map entries cover those.
- No changes to the political intra-market hunter (single rulebook — already covers
  Economics + Climate and Weather with no cross-venue risk).
- No Odds API involvement anywhere (nonsports never touches it).
