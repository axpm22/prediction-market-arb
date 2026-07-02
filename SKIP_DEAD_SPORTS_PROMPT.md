# Prompt for Claude Code — only scan/bet sports that actually have games

Copy everything below into Claude Code, run from the repo root.

---

Read `CLAUDE.md` and `SPEC.md` first. Task: **stop scanning and betting sports with no upcoming games.** Today every configured sport (`baseball_mlb`, `basketball_nba`, `soccer`, `tennis`, `mma`) is fetched every cycle even when it's out of season or has no fixtures — wasting poll time and, worse, **Odds API credits** (each consensus snapshot bills per sport) on sports that cannot produce a bet.

## Design — derive "in season" from the venues, never from a hardcoded calendar

A sport is **active** iff at least one *free* venue (Kalshi / Polymarket / SX — never the Odds API) currently lists a market for it with `start_utc` within a lookahead window. No hardcoded season dates, no static enable/disable lists — the venues' own listings are the calendar.

1. Add to `config.yaml` (no magic numbers, hard rule #4):
   ```yaml
   activity:
     lookahead_hours: 48        # a sport is active if any venue lists a game starting within this window
     recheck_dormant_mins: 360  # how often a dormant sport gets one cheap discovery probe
   ```
2. New helper (e.g. `arb/activity.py`, keep it under ~50 lines): given the latest free-venue discovery results, return the set of active sports. A sport with venue markets but all `start-unknown` or beyond the window is dormant.
3. Wire it in `arb/cli.py`:
   - **Watch/scan loop:** run free venue discovery for all configured sports on the dormant-recheck cadence, but only run the full quote fetch + detection for **active** sports each poll.
   - **Odds API consensus (`scan`) and the closing lane:** fetch **only active sports**. This is the credit saver — a dormant sport must never trigger an Odds API call. (`fetch_scores` for settlement is exempt: open bets must still settle even if their sport went dormant afterward.)
   - Log one line per skip: `skip-dormant-sport: basketball_mlb (no venue fixtures within 48h)` and record the skip in the poll's `venue_health_json`/notes so `status.sh` can show it — a sport silently missing must be distinguishable from a broken adapter (see K3 in `ArbModelV2_Plans/04 - Code Audit Findings (New).md`).
4. Tracker interaction (important, K3-adjacent): a sport skipped as dormant contributes no quotes, so its active fingerprints would be mass-closed as `absent-one-poll`. Exempt fingerprints of dormant-skipped sports from absent-closing that poll, exactly like a failed venue. In practice a dormant sport should have no active pregame fingerprints anyway — but do not let the skip manufacture CLOSE storms.

## Do NOT

- Do not hardcode league season dates or month ranges anywhere.
- Do not use Odds API `/sports` polling to decide activity (it costs credits and free venue listings already answer it).
- Do not remove any sport from `config.sports` — the config lists what we *would* trade; activity decides what we *do* trade this poll.
- Do not skip settlement (`settle`, reconcile, backfill) for sports with open bets.
- Do not touch the ledger or schema beyond the poll-notes/health field above.

## Acceptance

- `pytest` green, including a new unit test: discovery fixture with MLB games tomorrow + zero NBA markets → active = `{baseball_mlb, …}`, NBA skipped; and a fixture where NBA returns markets again → NBA reactivates within one dormant-recheck cycle.
- New test: a dormant-skip poll closes zero fingerprints of that sport.
- `python -m arb watch --once --dry-run` (live) prints the skip lines for out-of-season sports and shows no Odds API fetch for them; `x-requests-remaining` logged only for active sports.
- `status.sh` shows dormant sports as `dormant`, not as adapter failures.

Also sync `ArbModelV2_Plans/06 - Opus 4.8 Complete Build Guide.md`: add the `activity:` config block and one line in §6 ("sports gated on venue-derived activity; dormant sports cost zero credits and zero closes").
