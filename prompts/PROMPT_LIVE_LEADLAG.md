# Claude Code prompt — LIVE lane + LEAD-LAG study (logic spec, v1, 2026-07-08)

You are Claude Code working in the ArbitrageModel repo. Read `CLAUDE.md` and
SPEC.md §4 (hard rules) before touching anything. Both apply verbatim to this
work. This prompt adds two **measurement instruments** with pre-registered
kill gates: an in-play (LIVE) paper-bet lane and a lead–lag study (SPEC §"Where
edge might actually live", items 5–6). Neither ever touches the headline.
Both exist to be deleted if they fail their gate — a clean null is success.

**Precondition (rule 13):** the tree currently has uncommitted work
(`capacity.py`, `sizing.py`, dashboard/report edits). Get to a committed HEAD
with green `pytest` before Phase L1. Do not mix that work into these commits.

---

## Part 0 — Constraints that bind every phase

1. Paper only (rule 1). The LIVE lane books $1 paper flags; no order endpoint
   exists anywhere in this work.
2. **The Odds API is never called from the live loop** (rule 2). Live cadence
   would burn the 500-credit budget in an afternoon. Consensus/EV is
   pregame-only; the live lane runs on keyless venue endpoints exclusively
   (Kalshi REST, Polymarket CLOB, SX, MLB Stats).
3. Rule 12 stands: `arb/eligibility.py` is **not modified**. `classify()`
   already returns `(False, "live", "review")` for live rows — that IS the
   design. Live bets are review-lane forever unless a future dated
   pre-registration amendment creates a gate. No second predicate, anywhere;
   the existing grep-test must stay green.
4. Headline protection: the headline filter is trusted+sports+pregame+clean.
   Live rows are review-lane by (3), so they are excluded by construction.
   Add an assertion test anyway: a booked live bet must never appear in the
   headline query (`scripts/headline.sql` and `report.py` agree).
5. Every threshold below is a `config.yaml` key (rule 4). No magic numbers.
6. Fixtures-first (SPEC §6): record one real in-play response per venue into
   `tests/fixtures/` before writing any parser change; all tests run on
   fixtures.

---

## Part A — LIVE lane logic

### A1. The `live` flag becomes computed truth

Today `kalshi.py` and `polymarket.py` hardcode `live=False`; only `sx.py`
computes it. Fix: one shared rule, applied in each adapter (or a shared helper
in `venues/base.py`): `live = start_grade != "absent" AND start_utc <= now`.
A quote whose start can't be parsed stays `start-unknown` and is never live
(it is non-actionable everywhere, unchanged). No adapter may filter live
quotes out — rule 7 persists everything.

### A2. A dedicated poll kind: `live`

A new command `arb live` (and supervisor child `live`) runs its own cycle.
Poll-kind ownership (bug I1) then gives lifetime isolation for free: watch
bets and live bets never close each other.

- **Shortlist, not the board.** Each live cycle targets only events with
  `start_utc <= now < start_utc + live_lane.max_game_hours[sport]`, capped at
  `live_lane.shortlist_max_events`, prioritized by (events with open live
  bets, then most recent start). Sources: today's quotes table + MLB Stats
  schedule. The upper bound exists because V1 booked arbs on finished games —
  an event past the cap leaves the shortlist and a resolver saying FINAL
  records `FINAL_EVENT_SKIPPED`, never a flag.
- **Fetch scope:** Kalshi — orderbooks for shortlisted tickers only;
  Polymarket — CLOB books for shortlisted tokens only (batch endpoint per the
  existing §6.2 rule, budget + coverage fraction printed); SX — active
  markets for shortlisted leagues. Concurrent fetches, same timeout
  machinery as `watch`.
- **Cadence:** `live_lane.cadence_secs` (default 20). The achieved cadence is
  printed per cycle and in the daily report (cadence honesty, §13.1). All
  nulls from this lane are cadence-conditional claims, same as the
  pre-registration preamble.

### A3. Detection in live mode

`detect()` gains a `mode` parameter: `pregame` (default, behavior unchanged,
all existing tests untouched) and `live`:

- The in-play guard **inverts**: only groups with `start_utc <= now` are
  scanned; pregame groups are rejected `pregame-in-live-cycle` (diagnostic,
  not an error).
- The per-quote `q.live` rejection is skipped in live mode; everything else —
  zombie guard, empty-book trap, complement-map legality, covering-set proof,
  same-line totals, stale-warn ceiling, depth floor — applies identically.
  The draw-hole and covering-set rules do not relax in-play; a soccer game in
  the 80th minute still has three outcomes.
- **Simultaneity is tighter, not looser.** Live legs must be within
  `live_lane.max_actionable_skew_secs` (default 3) to be OK-status; the skew
  note and outer recording bound work as today. Skew decay uses
  `live_lane.line_velocity_points_per_sec` (per-sport, placeholders,
  explicitly marked for re-estimation by Part B — this is the same
  placeholder pattern v3.3 used for the pregame constants).
- Kind taxonomy unchanged: same-venue = ARB, cross-venue = XSPREAD, and every
  XSPREAD is REVIEW (rule 6). Additionally, **every opportunity from a live
  cycle carries `live=true`** into its candidate dict, and status floors at
  REVIEW (belt over suspenders — classify would review it anyway).

### A4. Tracker: booking, confirmation, settlement

- `candidate_from_opp` carries `live` from its legs (any live leg ⇒ live
  candidate). `process_poll` stops hardcoding `"live": False` and passes the
  candidate's value. This is the entire eligibility integration — classify
  does the rest.
- **Confirmation carve-out (requires a dated pre-registration amendment
  before the first live poll):** the v3.4 rule "an in-play second sighting
  never confirms" was written for pregame-booked bets drifting in-play, and
  it stands for them. For bets booked by the `live` poll kind, in-play IS the
  domain: a second consecutive live-cycle sighting of the same fingerprint
  writes a `confirmed` bet_event whose payload is tagged `live`. The
  live confirm rate is the lane's primary statistic — the in-play analogue of
  the 88% pregame vanish rate, measured at live cadence.
- Settlement mechanics are L1-identical: live ARB/XSPREAD lock at entry
  (`pnl_optimistic`), exempt from the instant-settle tripwire exactly as
  today. No new settler; the registered paths already cover the kinds.
- **Markout exclusion:** §10.5 marks are pregame-valid only (v3.4). Live bets
  take no markout rows; their honesty instruments are confirm rate, lifetime,
  and locked-vs-confirmed cost drift. State this in the module docstring so
  nobody "fixes" it later.
- `flat_bets` stays append-only (rule 9); nothing here writes to an existing
  row. The CI grep-test must stay green.

### A5. Reporting

Daily report gains an always-visible **LIVE lane** section (never blended):
flags booked, confirm rate, median lifetime, locked P&L (optimistic) vs
confirmed P&L, per venue-pair (populations never pool — kalshi:polymarket and
polymarket:sx live populations are pre-registered separately, like their
pregame counterparts), achieved cadence, shortlist coverage fraction. The
dashboard may mirror it read-only.

---

## Part B — LEAD-LAG study logic

Pure read-time analysis over stored quotes (rule 7 data — live cycles make it
dense, but watch data participates too). New command: `arb leadlag`.

### B1. Series construction

For each `(event_key, market_type, line, outcome, side, venue)` build the mid
series `m_v(t)` from stored quotes (`mid = (cost + bid)/2`, bid required;
quotes without bids are excluded and the exclusion fraction reported).
Complement-map before keying, same legality rules as detection — soccer NO
and integer-line NO never map.

### B2. Primary statistic — first-mover tally (robust, assumption-light)

- A **move** is `|Δmid| ≥ leadlag.min_move_points` between consecutive
  observations of one series.
- Two moves on the same proposition at two venues **match** when the same-sign
  move appears at both within `leadlag.match_window_secs`; the earlier
  `fetched_at` wins the pair.
- Per venue-pair (and per sport): `p_lead = wins_A / (wins_A + wins_B)`, CI by
  cluster bootstrap over `event_key` (the project's standard CI everywhere).
  A venue **leads** iff the 95% CI excludes 0.5. Also report the median lead
  time in seconds among won matches — that number is the tradability bound.

### B3. Secondary statistic — cross-correlation

Align both change series on a common poll grid; compute discrete
cross-correlation at lags `k ∈ [−K, +K]` polls (`leadlag.max_lag_polls`);
report `argmax` lag and the correlation there. Floats are permitted here
(statistical root-finding class, rule 5) with results re-quantized to 6-place
`Decimal` at storage.

### B4. Velocity re-estimation (the deliverable that feeds back)

Per sport and venue, emit observed live line velocity: median `|Δmid|/Δt` over
in-play observations. This replaces the placeholder
`live_lane.line_velocity_points_per_sec` constants via a normal config change
with `confirmed_on` dating — closing the loop v3.3 left open for the pregame
constants as well.

### B5. Persistence

Results append to a new `leadlag_runs` table (append-only; run timestamp,
config params echoed, per-pair rows as JSON). Reports read stored runs, never
recompute silently with different params (SC5 discipline).

---

## Part C — Pre-registration and kill gates (write BEFORE the first live poll)

Append to `PREREGISTRATION.md`, dated, before any live-lane data exists:

1. **Live lane hypothesis:** in-play cross-venue dislocations at ~20s cadence
   confirm at a non-trivial rate and carry positive locked P&L after fees.
   Populations: live kalshi:polymarket and live polymarket:sx, separate,
   never pooled with each other or with pregame.
2. **Live kill gate:** at the earlier of `live_lane.kill.max_days` (30) or
   `live_lane.kill.min_flags` (300) booked live flags per pair — if the live
   confirm rate < `live_lane.kill.confirm_rate_floor` (0.10) **or** the
   confirmed-P&L cluster-bootstrap 95% CI includes 0, set
   `live_lane.enabled: false` by dated amendment. Data is kept (it feeds
   Part B); the lane stops booking. Re-opening requires a new amendment.
3. **Confirmation carve-out amendment:** the A4 change to the v3.4
   in-play-confirm rule, scoped strictly to `poll_kind='live'` bets.
4. **Lead-lag hypothesis:** one venue's mids systematically lead another's on
   the same proposition. Gate: after `leadlag.min_matched_moves` (100) matched
   moves across ≥ `leadlag.min_events` (50) events for a pair — if no pair's
   `p_lead` CI excludes 0.5, record the null, close the study (dated
   amendment), and the honest successor claim is "no lead visible at ≥20s
   observation latency."
5. Nothing in this work creates a path to the headline. Any future
   live-to-clean gate is a separate pre-registered amendment with its own n.

---

## Phases (strict order; each ends with full `pytest -q` green + its acceptance command + a commit with evidence)

- **L1 — config + flag truth.** All new config keys under `live_lane` and
  `leadlag` (strict loader — orphan keys are errors, matching current
  discipline); adapters compute `live` per A1. Acceptance: a fixture-driven
  test shows a started event's quote arrives `live=true`, persists, and is
  rejected by pregame detect with reason `in-play` exactly as today.
- **L2 — detect live mode.** A3 on recorded in-play fixtures. Acceptance:
  live-mode detect on the fixture yields an XSPREAD with status REVIEW and a
  live tag; pregame-mode behavior byte-identical on existing fixtures.
- **L3 — live cycle + supervisor child.** Shortlist builder, scoped fetches,
  `arb live --once`, supervisor registration with its own
  `max_child_runtime_secs` entry. Acceptance: `arb live --once --dry-run`
  prints shortlist, achieved cadence, and coverage fraction against fixtures;
  no Odds API import anywhere in the live path (grep-test).
- **L4 — tracker + report.** A4 booking/confirm carve-out, A5 report section,
  PREREGISTRATION.md amendments landed. Acceptance: synthetic two-poll test —
  a live flag books review-lane at poll 1, confirms at poll 2 with a
  live-tagged event; headline query provably excludes it; `arb report --daily`
  unchanged except the new section.
- **L5 — leadlag.** B1–B5 plus one synthetic vector. Acceptance vector: venue
  A moves +0.02 at t, venue B follows +0.02 at t+2 polls (40s), 60 events →
  `p_lead(A) = 1.0`, CI excluding 0.5, argmax lag = 2, median lead 40s. And a
  null vector: simultaneous equal moves → `p_lead ≈ 0.5`, CI containing 0.5,
  study reports "no lead."

Workflow per CLAUDE.md: reproduce → failing test → minimal implementation →
full pytest → report unchanged except where stated → commit with evidence.
Modules stay under ~300 lines; money stays `Decimal`.
