# FIX: Polymarket MMA is invisible — wrong Gamma tag; enable cross-venue MMA paper betting

Read `SPEC.md` and `CLAUDE.md` first. All hard rules apply (paper trading only, no Odds API
credits in tests, Decimal money math, append-only `flat_bets`, quotes always persisted).

## Context — the bug (verified live 2026-07-02)

`arb/venues/polymarket.py` discovers markets via Gamma `GET /events?tag_slug=<slug>&closed=false`
with `TAG_SLUGS = {"mma": "mma", ...}` per SPEC §3.2.

That tag has rotted. Verified against the live Gamma API on 2026-07-02:

- `tag_slug=mma` → only futures-style events ("Who will become a UFC champion in 2026?",
  "Who will Merab Dvalishvili fight next?"). No `gameStartTime`, no recognized
  `sportsMarketType`, so `fetch_quotes("mma")` filters everything out and returns zero quotes.
- `tag_slug=ufc` → the real fight cards. Example: "UFC 329: Cody Durden vs. Alessandro Costa
  (Flyweight, Early Prelims)", `gameStartTime: 2026-07-11 21:00:00+00`, with market types
  `moneyline`, `totals`, plus `ufc_method_of_victory` and `ufc_go_the_distance` (out of v1 scope).

Consequence in production data: the last 48h of `quotes` shows MMA on Kalshi only. Cross-venue
MMA detection has silently never run, even though both Kalshi and Polymarket list the same fights.

## Constraint on "fixing forward"

CLAUDE.md rule 3 forbids inventing endpoints. `tag_slug=ufc` is NOT invented — it was verified
returning correct data on 2026-07-02 (see above). Update SPEC §3.2 as part of this change so
SPEC remains the single source of truth. Do not add any other tags speculatively.

## Tasks (in order)

### 1. Record fixtures (one-time live Gamma reads — free, unauthenticated, zero Odds API credits)

- Record `tests/fixtures/polymarket_mma.json` from the live
  `GET https://gamma-api.polymarket.com/events?tag_slug=ufc&closed=false` response (paginated,
  same params the adapter uses). Keep at least one full fight event containing `moneyline`,
  `totals`, `ufc_method_of_victory`, and `ufc_go_the_distance` markets so tests prove the
  out-of-scope types are ignored.
- Also record the current `tag_slug=mma` response as
  `tests/fixtures/polymarket_mma_futures_only.json` (regression fixture: proves the old tag
  yields zero quotes and documents why the tag changed).
- If a `polymarket_mma.json` fixture already exists from the old tag, replace it; note the
  replacement in the commit message.

### 2. Fix the adapter

- In `arb/venues/polymarket.py`, change `TAG_SLUGS["mma"]` from `"mma"` to `"ufc"` with a
  comment: `# Gamma's "mma" tag is futures-only since ~mid-2026; fight cards live under "ufc"
  (verified 2026-07-02).`
- The canonical sport string stays `"mma"` everywhere else — `event_key` prefix, `quotes.sport`,
  config keys, Odds API alias (`mma_mixed_martial_arts -> mma`). Only the Gamma query slug changes.
- Confirm the market-type filter still admits only `moneyline` (winner) and `totals` per SPEC §1,
  and silently skips `ufc_method_of_victory` / `ufc_go_the_distance`.
- MMA totals are ROUND totals (e.g. over/under 2.5 rounds). Verify the `line` value parses as
  Decimal and round-trips through the quote pipeline; do not special-case it unless a test fails.

### 3. Cross-venue matching sanity

- MMA events are fighter-vs-fighter; `event_key` is built from normalized names + start time.
  Pull the current Kalshi MMA event keys from `data/arb.sqlite` and check that the Polymarket
  fixture's fights would join (same fight on both venues ⇒ same key). Fighter-name spelling
  differs across venues more than team names do (e.g. "Dvalishvili" variants); add any needed
  entries to `data/aliases.json` rather than loosening the fuzzy-match threshold globally.
- Add a test: given one recorded Kalshi MMA quote and one recorded Polymarket quote for the same
  fight, the pipeline produces matching `event_key`s. Use fixtures only.

### 4. Activity gate

- `config.yaml` `activity_lookahead_hours: 48`: confirm MMA now flips ACTIVE when either venue
  lists a fight starting within the window, and that a UFC card weekend triggers Odds API
  consensus scans for `mma_mixed_martial_arts` only on those days (credit math in config.yaml
  §consensus_sports assumed ~48 credits/mo for MMA — re-check that estimate still holds with the
  ufc tag actually returning fights; adjust the comment if not).

### 5. Betting path — REVIEW vs auto-fill (CLAUDE.md rule 6)

"Make sure we can bet on it" means the PAPER ledger, and rule 6 says cross-venue opportunities
are REVIEW-only unless the venue-pair + market type is whitelisted in `config.yaml`.

- Do NOT blanket-whitelist MMA. Instead:
  a. Write up settlement-rule parity for Kalshi vs Polymarket UFC winner markets in a short
     section appended to `SETTLEMENT_RUNBOOK.md`: what each venue does on a draw, no-contest,
     doctor stoppage between rounds, fighter replacement/card change, and cancelled bout.
     Use each venue's published market rules (public pages; cite URLs). If any outcome case
     settles differently, the pair stays REVIEW-only — say so explicitly.
  b. If (and only if) winner-market rules are equivalent for draw/NC/cancellation, add
     `kalshi+polymarket / mma / winner` to the cross-venue whitelist in `config.yaml`, with a
     comment citing the runbook section. Leave totals (round totals) REVIEW-only regardless —
     round-total settlement on early stoppages is a known cross-venue mismatch risk.
- EV/consensus lane: with the tag fixed, MMA quotes will start flowing to the flat-stake tracker
  under existing measurement-mode rules. No changes needed; just confirm MMA EV flags appear as
  REVIEW while `measurement mode` is on.

### 6. SPEC update

- Amend SPEC §3.2's tag list: `mma` sport → Gamma `tag_slug=ufc`, with the 2026-07-02
  verification note. Keep the sport key `mma` unchanged.

## Acceptance checks

1. `pytest` fully green, including new tests:
   - `fetch_quotes("mma")` on the new fixture returns >0 winner quotes and >0 totals quotes;
     method-of-victory and go-the-distance markets produce zero quotes.
   - The futures-only fixture (`polymarket_mma_futures_only.json`) yields zero quotes without
     raising.
   - Kalshi/Polymarket same-fight `event_key` join test passes.
   - SX taker-cost and SPEC §8/§9 vectors still pass untouched.
2. Run one live `scan` (respect the credits guard). Show:
   - MMA quotes persisted from BOTH kalshi and polymarket in `quotes` for the next UFC card
     (2026-07-11 if scanning before then);
   - any cross-venue MMA opportunity surfacing as REVIEW (or auto-filled paper bet only if the
     whitelist criteria in Task 5 were met and documented).
3. `x-requests-remaining` logged; no test or loop consumed Odds API credits.

## Out of scope

- No new market types (method of victory, go the distance, rounds props).
- No changes to Kalshi/SX adapters.
- No new dependencies.
