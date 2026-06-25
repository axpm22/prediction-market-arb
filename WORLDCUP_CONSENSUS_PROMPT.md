# Claude Code prompt — season-aware consensus sports + Odds-API sport-key normalization

Paste everything below the line into Claude Code in this repo.

---

Read `CLAUDE.md` and `SPEC.md` first. This change must obey the hard rules (paper-only, public
GETs only, no invented Odds-API keys, no Odds-API credits in tests, config-driven thresholds,
Decimal money). Implement in small commits and make `pytest` pass.

## Problem

The EV lane only prices a venue quote when a consensus row exists for the **same** `event_key`
+ outcome. Consensus is gated by `consensus_sports` in `config.yaml`, currently
`[baseball_mlb, basketball_nba]`. Two issues make it MLB-only in practice:

1. **NBA is off-season** — it contributes 0 consensus rows and just holds a slot.
2. **Soccer / the World Cup is excluded** by a blanket budget comment ("soccer needs per-league
   key discovery"). That is true for club leagues but **false for the World Cup**: The Odds API
   exposes it as a single key, `soccer_fifa_world_cup` (verified at
   https://the-odds-api.com/sports/fifa-world-cup-odds.html), with the same cost profile as
   `baseball_mlb`. The venues (kalshi/poly/sx) are already quoting World Cup matches.

### The real blocker (must fix, or adding the key silently does nothing)

`arb/matching.py::event_key(sport, home, away, start, aliases)` uses the **raw `sport` string**
as the `event_key` prefix, and `arb/venues/oddsapi.py::parse_odds_events(events, sport)` passes
the **Odds-API sport key** as that `sport`. So scanning `soccer_fifa_world_cup` would build
consensus keys like `soccer_fifa_world_cup:2026-07-09:ecuador:germany`, while the venues build
`soccer:2026-07-09:ecuador:germany`. The two namespaces never join — consensus would be fetched
(spending credits) and match **zero** venue quotes. (MLB only works because the Odds-API key
`baseball_mlb` happens to equal the venue prefix.)

Confirm the venue-canonical soccer prefix is `soccer` (see `arb/venues/polymarket.py` sport map
and existing `soccer:...` rows in `data/arb.sqlite`) before coding.

## Goal

1. Normalize the Odds-API sport key to the **venue-canonical** prefix when building consensus
   `event_key`s, so consensus and venue keys join. Fetch with the real Odds-API key; key the
   event with the canonical prefix.
2. Update `consensus_sports` to in-season keys: drop `basketball_nba`, add
   `soccer_fifa_world_cup`.
3. Stay within the Odds-API 500/month budget; keep logging `x-requests-remaining`; use only the
   verified key (no guessing other soccer keys).

## Implementation

- Add ONE source of truth mapping Odds-API sport key → canonical prefix. Prefer config-driven
  per CLAUDE.md #4: a new `config.yaml` block, e.g.
  ```yaml
  # Odds-API sport key -> canonical event_key prefix (must match the venues' prefix so consensus
  # joins venue quotes). Identity for keys not listed.
  odds_api_sport_aliases:
    soccer_fifa_world_cup: soccer
  ```
  Load it in `arb/config.py` (default `{}`), and add a helper
  `canonical_sport(odds_api_key, aliases) -> str` (in `arb/matching.py` or a new `arb/sports.py`)
  that returns `aliases.get(key, key)`.
- Thread the canonical prefix into event-key construction:
  - The Odds-API **fetch** still uses the real key in the request URL and credit logging.
  - `parse_odds_events(events, sport)` (and the scan path that calls it) must build `event_key`
    with `canonical_sport(sport, aliases)` instead of the raw `sport`. Keep the raw key available
    for logging/diagnostics.
- Do NOT touch the venue adapters' prefixes — normalize the Odds-API side to match them.
- Update `config.yaml`:
  - `consensus_sports: [baseball_mlb, soccer_fifa_world_cup]`
  - fix the budget comment (still 2 keys × ~2 credits × 3 scans/day ≈ 360/mo, under 500).
- Optional (only if cheap and clean): allow a `consensus_sports` entry to carry an active window
  (`active_from`/`active_to`) so off-season keys are auto-skipped; a plain string list must keep
  working unchanged. Skip this if it complicates the diff.

## Tests (CLAUDE.md #8 — required, fixtures only, no live credits)

Add to `tests/`:

1. `canonical_sport("soccer_fifa_world_cup", aliases) == "soccer"`; identity for an unmapped key;
   `baseball_mlb` unchanged.
2. **The join regression** — the heart of the fix: build an Odds-API World Cup event (recorded
   fixture) and a kalshi (or poly) World Cup quote for the same teams/date; assert their
   `event_key`s are byte-equal (both `soccer:...`). This proves consensus will match venue quotes.
3. An end-to-end path test: a venue soccer quote priced under a World Cup consensus produces an EV
   opportunity in `detect_ev_from_consensus` (it produced zero before the prefix fix).
4. All existing devig / consensus / pricing test vectors stay green.

Use recorded fixtures in `tests/fixtures/`; never call the live Odds API in tests.

## Acceptance

- `pytest` is green.
- `python -m arb scan --fixtures` writes consensus rows with `soccer:` prefixes for World Cup
  events; a `python -m arb watch --once --fixtures` cycle yields EV flags matched to them
  (previously zero for soccer).
- Print/confirm the funnel: number of venue soccer quotes that now find a consensus match > 0.

## Out of scope / guardrails

- Paper-only; no authenticated or order endpoints; public GETs only.
- Don't add club-soccer/tennis/mma keys here — World Cup only, to respect the credit budget.
- Don't spend Odds-API credits in tests or loops; abort scan if `x-requests-remaining` < 50.
