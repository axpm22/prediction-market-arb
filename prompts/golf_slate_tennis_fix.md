# Task: add golf, add a daily 9am ET 2-day event slate, fix tennis fake edges

Read `CLAUDE.md` and `SPEC.md` first. All hard rules apply (paper only, no invented endpoints, fixtures-first tests, Decimal money math, append-only ledgers, no magic numbers). Three parts, in this order — part 1 is a correctness fix and comes first.

## Part 1 — Kill the tennis fake edges (bug fix)

Diagnosis from live DB: tennis EV bets carry `outcome=None`, a frozen pregame consensus `p_hat` (0.235101) reused across successive polls, asks sliding 8¢→11¢ poll-by-poll, all tagged `event-match-review` — and `live=0` even though the bets were placed while the match was in progress. Root cause of the mislabel: `live` is only set inside `break_windows`, which exists for NBA/soccer only, so in-play tennis quotes enter the pregame pipeline and get compared against pregame consensus. Fixes:

0. **Event-level start time, earliest venue wins.** Confirmed root cause (2026-07-02, Bublik–Jacquet): `split_pregame` already routes started events to the live lane, but it trusts each quote's own `start_utc` — and Polymarket Gamma `gameStartTime` for tennis is a nominal slot (follow-on court scheduling), hours later than the real start. Result: frozen 14:39 consensus vs a live price collapsing 0.22→0.03, booked as pregame EV with 100–340% "edges". Fix: compute per-event `start_utc = min(start_utc over all venues quoting the event)` during matching (Kalshi/SX starts are verified-reliable; the earliest claim wins because a too-early start is safe, a too-late one is the bug). `split_pregame` uses the event-level start. Additionally: for sports with unreliable venue start times (`unreliable_start_venues: {polymarket: [tennis]}` in config), a single-venue event whose only start source is the unreliable one is treated as `start unknown` → non-actionable lane (existing E5 behavior). Regression test: two-venue fixture where Polymarket says start=+3h, Kalshi says start=−10min → all quotes for the event live=true, zero EV bets even with a fresh consensus row.
1. **Never create an EV bet (or opportunity) when the quote's normalized `outcome` label is None/empty.** Consensus must join on `(event_key, outcome)` exactly — if the outcome can't be normalized to a known label, the quote is stored (rule: persist every quote) but excluded from EV detection. Log to `logs/unmatched.log` with reason `outcome-unnormalized`.
2. **Tennis (and any surname-matched sport) requires exact alias match for EV.** Fuzzy matches (`event-match-review`) may still produce DIVERGENCE records but never EV bets. Config: `ev_require_exact_match: [tennis, mma, golf]`.
3. **Edge sanity ceiling.** New config `ev_max_edge: 0.15` — computed EV above it means data error, not opportunity (SPEC §12 trap). Record the quote, create no bet, tag `edge-implausible` in a log line. No silent discards: count them in the daily report's "left on table" section under their own reason.
4. Regression test: fixture reproducing the observed case (consensus p̂=0.235101, Polymarket ask 0.09, outcome=None) asserts no EV bet is created; same fixture with a proper outcome label and exact match asserts the bet IS created.

## Part 2 — Add golf

1. Add `golf` to `sports` in `config.yaml`.
2. **Venue discovery — verify, don't invent (hard rule 3):**
   - Kalshi: re-run series discovery `GET /series?category=Sports`, find golf winner series in the response, extend `data/kalshi_series.json`. If none found, report and stop this sub-task.
   - Polymarket: try Gamma `GET /events?tag_slug=golf&closed=false&limit=100`. If empty/404, inspect available tags via one discovery call and report what exists — do not guess slugs.
   - SX Bet: `GET /sports`, read golf's actual sportId from the response (do not assume), then leagues/markets as usual.
   Record one real fixture per venue before writing parser code.
3. Golf markets are per-player outright binaries ("Will X win {tournament}?"). Matching: normalized player surname + tournament + event week, same alias mechanics as tennis (per-sport alias namespace). Golf is in `ev_require_exact_match` (part 1).
4. Settlement traps to encode in notes/REVIEW logic: playoffs, withdrawals (WD before start often voids on sportsbooks but NOT on prediction markets), 54-hole weather reductions. All golf cross-venue opportunities start `REVIEW` until the venue-pair is whitelisted in `trusted_pairs`.
5. `min_liquidity_usd` and `min_executable_usd` enforced strictly — golf books outside majors are thin; expect most weeks to produce nothing, that's fine.

## Part 3 — Daily 2-day slate, refreshed 9:00 AM ET

Purpose: stop scanning events that are days away; focus every poll on what's actually near.

1. New command `arb slate`: pulls upcoming events from all enabled venues (discovery endpoints only, keyless — never The Odds API), keeps those with `start_utc` within the next `slate.horizon_hours`, and writes the result to a `slate` table: `slate(id, built_at, event_key, sport, venue_count, start_utc)` plus a summary line per sport to the terminal. Rebuild = new rows with new `built_at`; latest `built_at` wins (append-only, consistent with everything else).
2. The watch loop reads the latest slate and **only matches/classifies events on it**. Events not on the slate: quotes may still be persisted if already fetched cheaply, but no matching/detection work. If the slate is missing or older than `slate.max_age_hours`, fall back to current behavior and log a warning — never silently scan nothing.
3. Daemon: schedule `arb slate` daily at `slate.refresh_time` (default `"09:00"`, interpreted in the existing `timezone: America/New_York` — 9 AM ET). Use the existing `schedule` library alongside the 23:59 report job.
4. Config:

```yaml
slate:
  enabled: true
  horizon_hours: 48
  refresh_time: "09:00"
  max_age_hours: 30    # stale slate -> fall back to full scan + warn
```

5. Tests: fixture with events at +6h, +47h, +49h → slate contains first two only; stale-slate fallback test; daemon schedule registration test (no live calls).

## Acceptance

1. `pytest` green, including the part-1 regression test and slate tests.
2. `arb slate` live run prints per-sport counts of events in the next 48h (golf included if in season).
3. One live `watch --once`: tennis produces zero EV bets with implausible edges; log shows any `edge-implausible` / `outcome-unnormalized` counts.
4. Show the daily report "left on table" section including the new reason buckets.
