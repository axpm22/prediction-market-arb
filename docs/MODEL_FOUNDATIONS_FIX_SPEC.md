# MODEL FOUNDATIONS FIX SPEC — the six least-examined assumptions, and how to close each one

Written 2026-07-02. Companion to SPEC.md; where they conflict, this document amends it.
Each phase below follows the same shape: **the assumption the model rests on → the evidence
it's shaky → the fix logic → what to build → the decision rule that the results force.**
The decision rules are the point: every phase ends with a pre-committed "if X, then we do Y"
so the results can't be rationalized after the fact.

Ordering is by evidentiary leverage, not ease. Phase A can invalidate half the system;
do it first. Phases are independent unless noted — they can be parallelized except where
flagged.

---

## Phase A — Is the consensus actually right? (EV premise validation)

**The assumption.** The entire EV lane assumes: de-vigged soft-book median = true probability;
venue price below it = edge. Every EV bet, edge bucket, and threshold-tuning table inherits
this.

**The evidence it's shaky.** MLB EV is −$18.46 on 27 settled $1 bets — a catastrophic ROI
that is either variance (27 bets is small) or the premise inverted. The consensus is built
from low-hold offshore books (lowvig, betonlineag, betus — labeled "sharp" in config but not
Pinnacle-class), de-vigged with the proportional method whose favorite-longshot bias the
config itself documents, and frozen up to 8 h between scans. Kalshi and Polymarket carry
real money from informed traders; it is entirely possible the venues are the sharp side and
"venue cheaper than consensus" systematically selects consensus error.

**The fix logic.** This is an empirical question with the data already in SQLite. For every
settled event that had both a consensus row and venue quotes at consensus time, we have three
probability estimates (consensus p̂, Kalshi mid, Polymarket mid) and one realized outcome.
Score all three as forecasters. Whoever has the lower Brier/log-loss is the sharp side.
Additionally score the *EV-selected subset*: on the bets the model actually flagged, did the
consensus side or the venue side win more often? That directly tests whether flags are edge
or anti-edge.

**Build.**
1. `arb regrade` command: joins settled outcomes to stored `consensus` rows and to venue
   quotes within ±5 min of `computed_at`. Emits per-source Brier score, log loss, and
   calibration table (predicted decile vs realized frequency), overall and per sport.
   Also regrades the three de-vig methods (proportional / power / shin) on the same data —
   the `devig_method` config choice becomes empirical instead of default.
2. The EV-subset test: for every settled EV bet, record whether the outcome favored the venue
   price or the consensus. Report win rate with a binomial CI.
3. Daily report gains one line: rolling 30-day Brier for consensus vs venue mids. The premise
   stays continuously monitored, not audited once.

**Decision rule (pre-committed).**
- Consensus beats venue mids on Brier AND EV-subset win rate > 50%: EV lane keeps running;
  tune `devig_method` to whichever wins the regrade.
- Venue mids beat consensus: **the EV lane's sign is wrong** — freeze EV bet creation
  (config `ev_enabled: false`), keep recording EV *quotes* for data, and repurpose the signal:
  consensus-vs-venue divergence becomes a *venue-leads-books* indicator to study, not bet.
- Inconclusive (CIs overlap): keep the lane but halve `ev_threshold` display prominence and
  re-run the decision monthly as settled N grows. No new EV-derived features until resolved.

---

## Phase B — Settlement-rule matrix (the named #1 risk, finally systematized)

**The assumption.** Two venues listing "the same event" pay out on the same outcome.

**The evidence it's shaky.** The spec calls settlement mismatch "the main source of fake
arbs" and then handles it with a REVIEW flag and vibes. Tennis retirements (weekly
occurrence) settle differently across venues; MLB has extra-innings/suspended-game wrinkles;
soccer has 90-minute-vs-extra-time scope; SX has explicit `outcomeVoidName` semantics the
others lack. `venue_settlement_lag_hours` still carries a "verify empirically" comment.
One retirement on a "trusted" tennis pair converts a locked arb into an unhedged option.

**The fix logic.** Make settlement equivalence a *data structure*, not a judgment call.
A cross-venue pair is only as trusted as the specific (sport, scenario) cells that have been
checked. Scenarios are enumerable and small: per sport there are perhaps 5–8 that matter.

**Build.**
1. `data/settlement_matrix.yaml`, human-maintained, structured as
   `sport → scenario → venue → {SETTLES|VOIDS|CONDITIONAL:<rule>|UNKNOWN}`. Seed scenarios:
   - tennis: retirement mid-match, retirement before start, walkover, suspended/resumed next day
   - baseball_mlb: extra innings, suspended game, postponement, doubleheader game-2 mixups, mercy/shortened
   - soccer: extra time scope, penalties scope, abandoned match, venue change
   - mma: no-contest, draw, weight-miss rebooking, card reshuffle
   - golf (when added): playoff, WD before/after start, 54-hole reduction
   Fill from each venue's published rulebook; every cell cites the rulebook section string.
   Unfilled = `UNKNOWN`.
2. Enforcement: a cross-venue opportunity is eligible for `trusted` **only if every scenario
   row for its sport has no UNKNOWN and no venue disagreement** for the venues involved.
   Any disagreeing cell → permanent REVIEW + note naming the scenario (`settle-mismatch:
   tennis-retirement`). This replaces the current whitelist-by-feel: `trusted_pairs` entries
   must now point at a fully-green matrix section to take effect.
3. Retro-check: `arb reconcile` (exists) gains a mode that, for every settled cross-venue
   bet, verifies both venues resolved the same direction; any mismatch writes a
   `settlement_mismatch` incident row and flips the matrix cell to the observed reality.
   The matrix thus self-corrects from lived settlements — the only ground truth that counts.

**Decision rule.** No cross-venue sport gets `trusted` until its matrix section is green.
If reconcile ever finds a mismatch on a trusted pair, the pair reverts to REVIEW immediately
and stays there until the matrix cell is amended with the rulebook citation explaining it.

---

## Phase C — Adverse selection: make paper fills earn their optimism

**The assumption.** A displayed ask can be taken at will; optimistic-vs-confirmed captures
the whole gap between paper and reality.

**The evidence it's shaky.** Both fill columns assume 100% fill probability with zero
market impact and zero information asymmetry. Reality: you get filled *easiest* precisely
when the quote-setter knows something you don't (the price is about to move through you).
The config's own `wr_ceiling` (win rates above 0.72 = "zombie-order inflation") is a patch
over the symptom. Paper P&L is currently an upper bound with an unmeasured bias.

**The fix logic.** Adverse selection is measurable without trading: it is the *conditional
drift of the price after you would have been filled*. If flags are informationless, the
post-flag price path is symmetric. If the books are picking us off, quotes we "took" drift
against us. The existing hot-watcher machinery already re-quotes fresh flags for 900 s —
the data source is built; the analysis isn't.

**Build.**
1. **Markout curves.** For every flat bet (EV and ARB legs), record the same outcome's best
   ask/bid at +30 s, +2 min, +10 min, +30 min after entry (hot watchers already sample this
   window; persist the markouts onto the bet row's analysis table, not the append-only log).
   Report median and mean markout per class, per venue, per edge bucket. A persistently
   negative markout at +2 min IS the adverse-selection cost, in cents, measured.
2. **Fill realism model.** Third P&L column, `pnl_markout`: entry price = the +30 s re-quote
   (not the flag-time quote). This is "you're never faster than 30 seconds" — a harsher and
   more honest assumption than `confirmed` (next-poll ≈ 60–163 s, but only if it persisted).
3. **Zombie-order detector.** Before counting a quote as fillable in any paper lane, require
   it seen at the same price in ≥2 consecutive polls OR flash-verified once. Unverified
   single-appearance quotes route to a `flicker` tag — recorded, never booked. (This subsumes
   the wr_ceiling heuristic with a mechanism.)

**Decision rule.** If markout at +2 min eats more than half the average flagged edge for a
class, that class's headline switches to `pnl_markout` as its primary number, and any future
"go live" consideration prices in the measured markout as a per-bet cost. If markout ≈ 0,
we have evidence the edges are real and slow — the single most valuable fact this project
could produce.

---

## Phase D — One supervisor, one heartbeat, no stale processes

**The assumption.** Six-plus loops (watch, political, ev-exit, focus, hot, negrisk, btc…)
running as independent processes against one SQLite file will all be running, current, and
mutually consistent.

**The evidence it's shaky.** Today's incident: a fix landed in the tree while the old daemon
kept betting for hours. Nothing knows what's running, at what code version, or whether a
loop silently died three days ago (dead `mma` scan would just… stop appearing).

**The fix logic.** Don't build process orchestration (no Docker, no systemd unit files, no
queues — YAGNI). Build *visibility* plus *one rule*: a single `arb supervisor` process owns
all loops as threads/subprocesses; anything else refuses to double-run.

**Build.**
1. `heartbeats(process, pid, git_head, dirty, started_at, last_beat_at, polls_done)` table;
   every loop beats each cycle (one UPDATE — this table is operational, not a ledger).
2. `arb supervisor`: starts every enabled loop from config
   (`processes: {watch: true, political: true, ev_exit: true, …}`), restarts a loop that
   stops beating for 3× its interval, logs restarts. Loops check a per-process lock (pid +
   liveness) so a second copy exits immediately with a message.
3. Staleness rule: supervisor records its `git_head` + dirty-hash at start; if the working
   tree hash changes, it prints one loud warning per change (no auto-restart — restarts are
   a human decision because tests may not have run).
4. `arb status`: one table — every process, alive/dead, beats, code version, last poll,
   last bet. The first command run every morning.
5. Report integration: daily report header shows any process that was dead >10% of the day —
   a day with a dead watch loop is not a comparable data day and gets flagged `partial-data`.

**Decision rule.** If any loop double-runs, dies silently, or runs stale code after this
ships, that's a Phase D bug, not an operational whoopsie. The daily number is only
publishable when the header is clean.

---

## Phase E — AI verdicts must expire when the rules text changes

**The assumption.** A Haiku equivalence verdict on a market pair is valid forever.

**The evidence it's shaky.** Venues edit resolution rules after listing — Kalshi amends
`rules_primary`, Polymarket clarifies descriptions after disputes. A cached `equivalent=true`
against rules that have since diverged is a silent fake-arb generator in exactly the
category (non-sports) where settlement wording is the whole risk.

**The fix logic.** The verdict isn't about the market pair; it's about the *rules texts*.
Key the cache by what was actually compared.

**Build.**
1. Cache key becomes `(market_a, market_b, sha256(rules_a), sha256(rules_b))`. Rules text is
   already fetched every poll for candidate pairs (Kalshi `rules_primary`, Gamma
   `description`); hashing is free. Text changed → cache miss → re-verdict (budget applies).
2. If a *previously-matched* pair's rules hash changes: the pair drops to unmatched
   immediately (fail-safe), a re-verdict is queued, and open paper bets on it are annotated
   `rules-changed-midflight` (Phase C markout analysis excludes them; settlement watches
   them specially — a mid-flight rules edit on one venue is the purest settlement-risk
   specimen we can collect).
3. Negative verdicts also re-check on text change — a venue clarifying its rules can *create*
   equivalence, and those pairs are free inventory.
4. `nonsports_verdicts` gains `rules_hash_a/b` columns; append-only discipline unchanged.

**Decision rule.** Any settlement mismatch on an AI-matched pair whose rules changed
mid-flight and wasn't caught by this mechanism is a sev-1 against the matcher; the venue
pair moves to human-map-only until root-caused.

---

## Phase F — Time gets the same rigor as money

**The assumption.** Timestamps are roughly right and roughly comparable: venue start times
are true, consensus age is fine, "60 s loop" means 60 s.

**The evidence it's shaky.** Every incident this project has had was a clock problem:
Gamma's fictional tennis start times (today), frozen consensus vs live prices (today),
watch cadence actually ~163 s vs the configured 60 (measured from poll gaps), legs fetched
up to a minute apart pretending simultaneity.

**The fix logic.** Same treatment money got: a single canonical representation, validation
at the boundary, and honest propagation of uncertainty. Time values get a *reliability
grade*, and every consumer must handle the ungraded case explicitly.

**Build.**
1. **Start-time truth hierarchy** (generalizes today's tennis fix): per event,
   `start_utc = min over reliable sources`, with reliability configured per (venue, sport)
   — `unreliable_start_venues` already exists; extend it to a graded map
   (`verified | nominal | absent`). `nominal`-only events are non-actionable (already the
   E5 lane). Where a schedule API is free and keyless for a sport (e.g. MLB stats API is
   already an adapter), it outranks all venue times.
2. **Consensus age on every EV row.** `consensus_age_secs` stored per bet at creation, and
   the edge-bucket report gains an age dimension. If Phase A shows edge decays with age
   (it will), `consensus_max_age_hours` stops being a guess.
3. **Loop-cadence honesty.** Each poll records planned vs actual interval; daily report
   shows p50/p95 cycle time per loop. The 60 s config number either becomes true (Phase D's
   supervisor can parallelize venue fetches — they're independent HTTP calls) or the config
   is corrected to reality. No silent 2.7×.
4. **Leg-skew budget.** ARB legs already force REVIEW beyond `max_leg_skew_secs`; add the
   measured skew to every multi-leg record so the flash-verify pass can be prioritized by
   skew instead of FIFO.

**Decision rule.** After F ships, any bet created from a `nominal`-graded start time or an
unmeasured consensus age is a bug. The p95 cycle time printed in the daily report is the
loop's real spec; claims about "we scan every N seconds" cite it, not config.

---

## Cross-cutting rules

- Every phase lands with its regression tests (fixtures only, no live calls, no API credits)
  and its acceptance run output pasted into the phase's commit message.
- Nothing here relaxes CLAUDE.md hard rules: paper only, Decimal money, append-only ledgers,
  quotes persisted always, headline = trusted+sports+pregame.
- Analysis tables (markouts, heartbeats) are operational data and may UPDATE; anything that
  feeds P&L stays append-only.
- Each phase's decision rule is written down *before* its data comes in — this file is the
  preregistration. When a decision fires, record the trigger numbers in PREREGISTRATION_V2.md
  alongside the action taken.

## Suggested order of execution

A (one command, uses existing data, can kill/validate the biggest lane) →
D (stops the operational bleeding; today's incident class) →
B (unblocks any future trusted/real-money path) →
C (turns paper P&L into evidence) →
E (small, sharp, closes the AI matcher's worst hole) →
F (partly shipped already via the tennis fix; finishes the discipline).

A and D are independent and can be two parallel Claude Code sessions in separate worktrees.
