# Task: close out the in-play betting incident (2026-07-02)

Read `CLAUDE.md` and `SPEC.md` first. All hard rules apply — especially: paper only, `flat_bets` append-only (never update/delete rows), no magic numbers, fixtures-only tests.

## Incident summary (context, verified against the live DB)

Tennis EV bets were booked during live matches with fake edges up to 340%. Chain: Polymarket Gamma `gameStartTime` for tennis is a nominal follow-on slot hours after the real start → `split_pregame` trusted the per-quote start → in-play quotes stayed `live=0` → live collapsing prices (Bublik–Jacquet, 0.22→0.03) were compared against a consensus frozen at 14:39 UTC → a new "pregame EV" bet nearly every poll. Corroborating tell: Odds API delisted the match at 17:21 (books pull markets at first serve) while Polymarket/Kalshi kept quoting.

A working-tree fix already exists (event-level start = earliest reliable venue in `split_pregame`, `unreliable_start_venues` config, `tests/test_tennis_ev_fix.py`). All processes are currently stopped via `data/stop-all`. Your job: verify that fix, add the missing last-line defenses, quarantine the bad rows in the report, and leave the system safe to restart.

## Steps

### 1. Verify and commit the existing fix

Run the full `pytest`. Read `split_pregame` (arb/cli.py) and `event_start_map` and confirm: (a) event-level start = min over venues NOT listed in `unreliable_start_venues` for that sport; (b) an event whose only start source is unreliable → non-actionable lane; (c) `tests/test_tennis_ev_fix.py` covers the two-venue case (Polymarket says +3h, Kalshi says −10min → all quotes live=1, zero EV bets). Fix anything missing. Commit with a message referencing this incident.

### 2. Tracker-boundary invariant (belt and braces)

`split_pregame` is upstream routing; nothing today stops a mislabeled quote that slips past it from being booked. Add a final guard at bet-creation in `tracker.py`: before inserting any EV or ARB row, recompute `event_start <= now` from the event-level start map; if the event has started (or start is unknown), refuse the insert, log one line `inplay-bet-refused fingerprint=… event=…`, and count refusals in the daily report's data-quality section. This is a cheap invariant check, not a second routing system — one function, called at one choke point. Test: hand the tracker an opportunity whose event started 1 min ago → no row inserted, refusal logged.

### 3. Quarantine today's bogus rows — without touching them

`flat_bets` is append-only, so the bad tennis rows stay. Add an append-only `bet_annotations(id, bet_id, annotated_at, reason, notes)` table. Write a one-off command `arb annotate --reason inplay-mislabeled --where "<criteria>"` that inserts annotations for the affected rows (tennis EV bets placed 2026-07-02 between 14:00 and 19:01 UTC on events with `event-match-review` + implausible edges — derive the exact set from the DB, print it, and require a `--yes` flag before writing). Report change: annotated bets are excluded from every P&L section and shown only in a "voided — data error" line with count and reason. `report --daily` recomputation joins the annotations table (idempotent, derived from logs as always).

### 4. Stale-daemon guard

The fix was in the tree while the OLD process kept betting — code changes don't reach a running daemon. Minimal fix: on startup, `daemon` (and `watch`) write `data/daemon.meta` (pid, started_at, `git rev-parse HEAD`, dirty-tree flag). Every poll cycle, if HEAD or the dirty flag no longer matches, print a loud warning `code changed under running daemon — restart to pick it up` (once per change, not per poll). No auto-restart — just make staleness visible. Test with a fake meta file.

### 5. Restart

After all tests pass: delete `data/stop-all`, run one `watch --once`, and show that (a) in-play tennis quotes are persisted `live=1`, (b) zero EV bets on started events, (c) any `inplay-bet-refused` count is visible. Then state clearly that the daemon can be restarted.

## Acceptance

1. Full `pytest` green.
2. `watch --once` output demonstrating in-play quotes routed live=1, no bets on started events.
3. The annotation run's printed row set + the daily report showing the "voided — data error" line and headline unchanged.
4. `git log` showing the fix + guards committed; `data/stop-all` removed only after everything above passes.
