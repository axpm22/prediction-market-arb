# BUILD: Live baseball EV — Phase 0 feasibility probe + Phase 1 candidate recorder

Read `SPEC.md`, `CLAUDE.md`, and **`LIVE_BASEBALL_EV_SPEC.md` in full** before any code. The
live spec is the source of truth for this lane; this prompt sequences the work and pins the
acceptance gates. All hard rules apply. This lane is MEASUREMENT-ONLY: `betting_enabled:
false` is not touched by this work, and nothing here books a paper bet — Phase 1 records
candidates, not positions.

## Current state (verified 2026-07-02)

- `arb/experimental/live_baseball_ev.py` (212 lines): the pure core EXISTS and is good —
  `LiveSnapshot`, `freshness_rejection`, `stability_rejection`, `edge_ladder`,
  `evaluate_live_candidate`, `recheck`, `record_live_candidate`. Do not rewrite it; wire it.
- What does NOT exist: any runner. No Phase 0 probe, no live polling loop, no CLI entry, no
  live play-by-play fetch (`arb/venues/mlbstats.py` only has `final_games` for settlement).
- `config.yaml` `live_baseball_ev:` block exists; freshness numbers are PLACEHOLDERS pending
  Phase 0 (the comments say so — keep them honest).
- `hot_samples` table exists and is empty.

## Hard constraints for this lane (beyond CLAUDE.md)

1. **Credits are the blocker (spec §5.1).** Live Odds API polling at seconds-cadence would
   burn the 500/mo cap in hours. Phase 0 must FIRST compute the credit math and print it:
   live consensus via Odds API is allowed only within an explicit per-day budget
   (`live_baseball_ev.max_odds_api_calls_per_day`, add to config, suggest 20 — enough to
   sample "how many live books exist" a few times per game, nowhere near enough for a
   consensus feed). Venue quotes (Kalshi/Polymarket/SX) and MLB Stats are free — poll those
   as fast as politeness allows.
2. **Clock discipline (spec §1.4).** Every age is `our_monotonic_fetch_time` based. Never
   difference two feeds' embedded timestamps. Feed-embedded times are used ONLY to estimate
   publish lag as a distribution.
3. **Endpoints**: MLB Stats live feed endpoint per the spec's inputs section (§1.3). Verify
   it works with ONE manual call and record a fixture before the loop uses it (rule 3 — and
   if it 404s, stop and report; do not guess alternatives). MLB Stats is free and
   unauthenticated.
4. **Inherit the rebook-loop lessons** (FIX_SETTLED_REBOOK_LOOP_PROMPT.md): candidate rows
   must be deduped per (fingerprint, game-state), a game that has ENDED must never produce a
   candidate (check MLB Stats game status on every cycle), and anything that would "settle"
   within one poll of being recorded is a bug tripwire, not a datapoint.
5. Modules under ~300 lines; runners live in `arb/experimental/` next to the core.

## Phase 0 — feasibility probe (build this first, ship it alone)

New runner: `python -m arb.experimental live-baseball-probe --games N --minutes M`.

For 1–2 live games (pick from today's MLB schedule via the existing schedule source), poll on
a fixed cadence (config `live_baseball_ev.probe_poll_secs`, default 10) and RECORD ONLY:

1. **MLB Stats publish lag**: for each play/pitch event, `our_fetch_time − feed_embedded_time`
   (labeled clearly as lag-estimate, subject to clock skew — that's fine, it's a distribution,
   the median offset IS the effective lag for gating purposes).
2. **Venue quote behavior per poll**: top bid/ask/depth for the game's winner (and totals if
   listed) markets on Kalshi/Polymarket/SX; whether the market is suspended; time-to-reopen
   after suspension events.
3. **Live book availability** (Odds API, within the daily budget): at a handful of sampled
   moments, how many live MLB books the feed actually returns, and their prices. Log
   `x-requests-remaining` on every call; abort probing below 50 remaining (existing rule).
4. **Depth reality**: distribution of top-of-book live depth vs the pregame `$50` executable
   assumption.

Persist raw samples append-only (new table `live_probe_samples` or reuse `hot_samples` if its
schema fits — prefer reuse, check first). Then a report command
`python -m arb.experimental live-baseball-probe --report` that prints histograms/percentiles:
publish lag p50/p90/p99, quote age p50/p90, suspension frequency and reopen times, live book
count distribution, depth distribution. **The report must end with a verdict line per §5:**
are `max_state_age_secs: 5` and `$50` depth achievable, and what values does the data
support? Update the config placeholders with measured values (and delete the PLACEHOLDER
comments) only after this report exists.

**Phase 0 acceptance:** probe run on ≥2 real games (free feeds; ≤ the Odds API daily budget),
report shown with the verdict, config updated from measurements. `pytest` green with fixture
tests for the probe's parsing (recorded MLB Stats live fixture; venue fixtures).

## Phase 1 — candidate recorder (only after Phase 0's verdict is viable)

If Phase 0 says freshness/depth targets are unachievable, STOP and report — do not build
Phase 1 with numbers the data refuted. Otherwise:

New runner `python -m arb.experimental live-baseball-watch`:

1. Game selection: live MLB games from the schedule; skip games in a terminal state.
2. Each cycle, per game: fetch game state (MLB Stats) and venue quotes; build `LiveSnapshot`s
   with all ages from OUR fetch clock and `effective_state_age = state fetch age + measured
   publish lag (Phase 0 median)`.
3. Fair value: median de-vigged live sportsbook probability across ≥3 live books on the SAME
   market — but given the credit budget, expect this to be rarely available; when it isn't,
   record the snapshot with `fair_value=None` as a NO-CONSENSUS diagnostic (still valuable:
   it measures how often the lane could even run). NO win-probability model in v1 (spec §1.6
   — it's not cleared, it may not set fair value).
4. Run `evaluate_live_candidate` (existing core). Statuses per the spec: CANDIDATE /
   rejected-with-reason. For CANDIDATEs, schedule the `recheck` with a FRESH re-fetch
   (spec §0.2 — never reuse the entry snapshot), producing CONFIRMED-CANDIDATE / VANISHED.
5. `record_live_candidate` (existing) writes every evaluation append-only with full state,
   including rejections — the rejection mix IS the product of Phase 1.
6. Report: `--report` prints candidate counts by status, VANISHED rate, rejection-reason
   histogram, and the no-consensus rate. No P&L anywhere — nothing is bet.

**Phase 1 acceptance:** watch run through ≥1 full live game day; report shown; zero rows in
`flat_bets` from this lane (assert it — measurement only); rejection histogram non-trivial
(if 100% of snapshots pass the gates, the gates are wired wrong); `pytest` green — core
functions get fixture-driven tests for: a mid-play snapshot rejected, a stale-state snapshot
rejected, a suspended-market snapshot rejected, a stable between-batters snapshot accepted,
recheck VANISHED on a moved price, recheck must-refetch enforced (reusing the entry snapshot
is a test failure), ended-game produces nothing.

## Explicitly out of scope

- Phases 2–3 (calibration, promotion) — they need Phase 1 data that doesn't exist yet.
- Any betting, paper or otherwise, from this lane. `betting_enabled` stays false.
- Live win-probability model.
- Tennis (separate prompt; different feed reality).
- Any new Odds API usage beyond the explicit daily probe budget.

## Workflow

Phase 0 is a complete, reviewable deliverable on its own — commit it, run it on tonight's
games, show the report, and STOP for review before Phase 1. That's the repo's phase
discipline, and Phase 1's constants come from Phase 0's output, so building both blind in one
pass would just bake in new placeholder numbers.
