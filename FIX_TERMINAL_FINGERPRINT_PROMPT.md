# Claude Code prompt — fix the `terminal-fingerprint-skipped` regression that suppresses EV/ARB flags

Paste everything below into Claude Code from the repo root.

---

You are working in the ArbitrageModel repo. **Read `CLAUDE.md` first** — hard rules override
anything here (paper-only, public reads, Decimal money math, **append-only ledger — never
edit or delete rows**, every threshold in `config.yaml`). This is a bug fix plus a regression
test. Diagnose, confirm against the ledger, fix narrowly, prove it.

## Symptom

Clean (un-quarantined) EV and ARB flags collapsed to ~zero around June 24–25 2026, even
though raw detection volume stayed high. The scanner is still *finding* opportunities; almost
all are being rejected **after** detection.

## Evidence (already gathered — re-confirm, don't trust blindly)

Querying `data/arb.sqlite` `flat_bets`:

- Clean EV/ARB per day were healthy through 6/22–6/23 (hundreds/day), then: 6/24 → 0 clean,
  6/25 → 593 EV flags but **0 clean** (330 `terminal-fingerprint-skipped`, 215 `below-e-min`,
  48 `duplicate-position-skipped`); ARB 3 clean vs 521 two days earlier.
- `terminal-fingerprint-skipped` first appears `2026-06-23T07:47` (introduced in the June 23
  audit-remediation commits) and totals ~376 rows.
- Smoking gun: fingerprint `4edf0eda…` has a `SETTLED`/`LOST` row at `2026-06-25T05:23`, then
  was re-sighted and skipped **101 times the same day**.

`below-e-min` is **not** the cause — those are genuine edges under the `ev.e_min` floor and
were always present. The collapse is entirely the terminal-fingerprint skip.

## Root cause (confirm in code)

In `arb/tracker.py`, when a fresh opportunity appears (`row is None`, i.e. no currently-active
tracker row for that fingerprint), it calls `_latest_hard_terminal(con, fingerprint)`:

```python
WHERE fingerprint = ? AND status IN ('SETTLED','EXITED')
```

If that fingerprint has **ever** reached `SETTLED` or `EXITED`, the new sighting is rejected
as `terminal-fingerprint-skipped` (see the `rejection = (…, "terminal-fingerprint-skipped", …)`
block, ~lines 56–64). Because terminal rows accumulate monotonically (every settle run, every
ev-exit), the muted-fingerprint set only grows and within ~a day swallows essentially all
flags. Two specific defects:

1. **It mutes forever**, conflating "don't re-open a position that's *currently still active*"
   (correct, already handled by the active-row check and `duplicate-position-skipped`) with
   "never record this opportunity again once it has *ever* settled/exited" (the regression).
2. **`EXITED` counts as terminal**, so the ev-exit watcher closing a position mid-life
   permanently mutes that opportunity for the rest of its real pregame window — even though a
   later re-sighting is a legitimate new entry.

Note `fingerprint = opportunity_fingerprint(kind, event_key, legs)` and `event_key` already
includes the event date, so cross-day collisions aren't the issue — premature/own-exit
settlement during a still-live market is.

## Required behavior

- A re-sighting of an opportunity whose **only** terminal history is from a *prior, completed
  lifetime* must be allowed to **re-open as a new bet via the existing `reappearance_of`
  path** (`_latest_closed_id` / `reappearance_of`, ~line 92), not silently skipped.
- Preserve the anti-inflation intent (Weakness #4): still must NOT double-open a position that
  is **currently active** in the same poll/window — that protection stays (active-row check +
  `duplicate-position-skipped`).
- Still correctly skip a sighting that arrives **after the event is genuinely over** (don't
  resurrect a market whose game has started/finished).

## Recommended implementation (verify before committing)

First read the commit that introduced `_latest_hard_terminal` (`git log -S terminal-fingerprint
-- arb/tracker.py`) to recover the original intent, then implement the **minimal** change that
satisfies the behavior above. Recommended:

1. **Drop `EXITED` from the hard-terminal mute** — an exited position should reappear as a new
   bet, not be permanently silenced. (`EXITED` already has a reappearance path via
   `_latest_closed_id`.)
2. **Bound the `SETTLED` mute to events that are actually over.** Only skip when the
   opportunity's event start (`opp.event.start_utc` / leg `start_utc`) is in the past by at
   least a configurable grace — i.e. the game has begun/finished, so a new sighting really is
   post-event noise. If the event is still in the future, a `SETTLED` row is premature/own-
   settlement and the sighting must route to the normal open/reappearance path.
3. Put any new time window in `config.yaml` (e.g. `tracker.terminal_mute_grace_mins`), no
   inline constant. Default it to behavior that restores clean-flag flow.

Keep the change surgical and the module under ~300 lines. Do not touch the append-only
history; this is forward-only.

## Regression tests (add to `tests/test_tracker.py`)

Use the existing in-memory `Store` test fixtures. Add cases:

1. **Reappears after settle (the bug):** track an opportunity (event start in the future) so it
   opens → write a `SETTLED` row for that fingerprint → track the *same* opportunity again in a
   later poll. Assert the new row is `OPEN` with `reappearance_of` set, and **not**
   `terminal-fingerprint-skipped`.
2. **Reappears after exit:** same as above but with an `EXITED` row instead of `SETTLED`.
   Assert it re-opens.
3. **Still muted post-event (no regression of the valid case):** event start in the *past*
   beyond the grace, fingerprint `SETTLED` → a new sighting is still skipped
   (`terminal-fingerprint-skipped`).
4. **No double-open (preserve Weakness #4 fix):** an opportunity with a currently-**active**
   OPEN row, re-sighted in the same poll, is not opened twice (duplicate-position/active-row
   path still rejects it).

## Acceptance / verification

- `pytest` green, including the four new cases.
- Re-run the relevant scan against **recorded fixtures only** (never spend Odds API credits;
  honor the `x-requests-remaining` abort rule) and show that clean EV/ARB flags are produced
  again where a settled/exited prior instance exists.
- Print a before/after of the skip-reason mix on a replayed poll to show
  `terminal-fingerprint-skipped` no longer dominates.
- `git diff` touches only `arb/tracker.py`, `config.yaml`, and `tests/test_tracker.py` (plus
  config plumbing in `arb/config.py` if you add the grace key). No ledger rows mutated.

## Out of scope

No change to the `below-e-min` / `ev.e_min` floor (working as intended), no detection-side
changes, no DB migration, no edits to historical rows.
