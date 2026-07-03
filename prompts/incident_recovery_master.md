# Task: full recovery from the 2026-07-02 incidents — fix, verify, reset, restart clean

Read `CLAUDE.md` and `SPEC.md` first. All hard rules apply. This prompt sequences the two
pending fix prompts, adds the missing tripwire, resets the tracker count, and gates the
restart. Do the steps IN ORDER; each has its own gate. Do not skip a gate because a later
step "will catch it."

## Context (verified against the live DB, 2026-07-02 ~20:25 UTC)

The tracker printed fake money all day. Two mechanisms, compounding:

1. **Poll-interleaving dedup bug** — non-watch polls (political, ev-exit, focus, negrisk)
   close every watch fingerprint each cycle, so the same market re-books as a "new" $1 bet
   every ~165 s. Fingerprint `c2f39d1b…` has ~40+ rows.
2. **Ended/in-play games booked as pregame** — bets placed at 20:16:30.38 settled WON at
   20:16:30.74 (0.4 s later): the game was already decided at booking; a stale 47¢ ask on
   the winner became "+$1.10 profit" every poll. Optimistic P&L: $17.12 and climbing ~$24/hr
   before the stop. All junk. Confirmed P&L $2.23 (also polluted).

`data/stop-all` is set — everything is stopped. The daemon was restarted once today WITHOUT
the fixes (tracker.py was modified at 20:19 UTC, after the process started), which is why
the bug kept running after being diagnosed. Do not let that happen again: nothing restarts
until every gate below is green.

## Step 1 — run the two pending fix prompts, in this order

1. `prompts/stop_inplay_betting_fix.md` — SKIP its step 5 (restart); restart is gated at the
   end of THIS prompt instead. Everything else applies: verify+commit the event-level start
   fix, the tracker-boundary in-play refusal, `bet_annotations` table, stale-daemon guard.
2. `prompts/fix_poll_interleaving_dedup.md` — the same-poll-kind rule for lifetime/dedup/
   confirmed-fill, its regression tests, and its duplicate-detection annotation pass.

Where the two prompts both define `bet_annotations`, build it once (identical schema).

**Gate 1:** full `pytest` green, including both prompts' regression tests and the untouched
SPEC §9 tracker vector.

## Step 2 — the settle-tripwire the incident proved missing

New invariant in the settlement path: **a bet whose `settled_at − placed_at` is less than
one watch interval is a data error, never a result.** Config: `settle_tripwire_secs`
(default = `watch_interval_secs`). On trip: the settlement still writes (append-only), but
the row is auto-annotated `settle-tripwire` in `bet_annotations` in the same transaction,
excluded from every P&L section, and counted in the data-quality section. A tripped
fingerprint is also blacklisted from re-booking for the rest of its event's lifetime (the
rebook loop dies even if some future bug re-opens the front door).

Test: fixture where a bet settles 1 s after placement → annotated, excluded, fingerprint
not re-bookable; a bet settling 2 h after placement → untouched.

**Gate 2:** `pytest` green including the tripwire tests.

## Step 3 — reset the count (user decision, 2026-07-02)

The displayed P&L restarts from zero. `flat_bets` rows are never edited or deleted (hard
rule 9), so the reset is a **baseline cutover**, same pattern as the v2 cutover on
2026-07-01 (`data/arb-v2-baseline-20260701.sqlite`):

1. Stop-flag still present (verify — abort if someone removed it).
2. Copy `data/arb.sqlite` → `data/arb-incident-baseline-20260702.sqlite` (read-only archive;
   the 500k+ quotes stay queryable for backtests and for Phase A regrade work).
3. Initialize a fresh `data/arb.sqlite` via the existing schema/migrations path (empty
   `flat_bets`, `polls`, `opportunities`, …). Aliases/series caches and `nonsports_map.yaml`
   are files, not DB — untouched.
4. `arb report` on the fresh DB shows zeros everywhere; the dashboard reads $0.00.
5. Record the cutover in `PREREGISTRATION_V2.md`: date, reason (incident: interleaving dedup
   + ended-game rebooking made all prior tracker P&L unusable), archive filename, and the
   rule that pre-cutover data is analysis-only, never blended into post-cutover P&L.

**Gate 3:** fresh DB passes `arb report` (all zero), archive file exists and opens.

## Step 4 — restart, supervised

1. Run one `watch --once`. Show: in-play/ended events routed live=1 or refused
   (`inplay-bet-refused` counter visible), no bet settles within the tripwire window, no
   `reappearance_of` chains for fingerprints present on consecutive watch polls.
2. Only then delete `data/stop-all` and start the daemon + any experimental processes.
3. Watch the first 15 minutes of polls (script a check, don't eyeball): if ANY bet is
   created with `settled_at − placed_at < settle_tripwire_secs`, or any fingerprint books
   twice within 3 watch polls, touch `data/stop-all` again and report failure loudly.

**Gate 4 (final acceptance):** 15-minute live soak clean — zero tripwire hits, zero
duplicate bookings, tennis/ended-game events visibly in the live lane, dashboard starting
from $0.00. Paste the soak-check output into the commit message.

## Out of scope

- MODEL_FOUNDATIONS_FIX_SPEC.md phases (A–F) — separate work, don't bundle.
- Any change to detection math, thresholds, or venue adapters beyond what the two fix
  prompts specify.
