# Claude Code prompt — EV re-open path + weighted-ARB (edge × depth) enablement (v1, 2026-07-08)

You are Claude Code working in the ArbitrageModel repo. Read `CLAUDE.md`,
SPEC.md §4, and `PREREGISTRATION.md` before touching anything; all three bind
verbatim.

**What this prompt is NOT.** It does not set `ev_enabled: true` or
`arb_size_by_ladder: true` directly. Both switches are frozen by
pre-registration ("changing any rule after data collection starts is a dated,
versioned amendment — never an edit"). Silently flipping either would repeat
the exact failure class the rebuild exists to prevent. What you build instead
is the complete activation machinery: the measurement commands, the gate
evaluators, and the amendment mechanics, so that each switch flips the moment
its own pre-registered trigger passes — and cannot flip any other way. If the
owner wants to force a flip without a passing gate, that is a human-authored
dated amendment in PREREGISTRATION.md; nothing in this prompt implements a
bypass.

**Precondition (rule 13):** committed HEAD, green `pytest -q`. One commit per
phase, failing-test evidence in the message. Fixtures only in tests (rule 2).
Money `Decimal` (rule 5); every new threshold in `config.yaml` (rule 4);
`eligibility.py` untouched (rule 12) — both gates already exist inside
`classify()` (`ev-frozen`, and capacity is projection-side); nothing below
adds a second predicate.

---

# Part A — EV re-open path (PREREGISTRATION.md "EV lane", SPEC §18.3)

The lane is frozen on an inherited verdict (consensus Brier 0.238 vs venue
mids 0.221/0.223; EV-subset win rate 420/877 = 0.479). The re-open trigger is
already written down; what's missing is the machinery to evaluate it:
`arb regrade` is declared in the CLI (`arb/cli.py:63`) but `cmd_regrade` does
not exist — `getattr(commands, "cmd_regrade", None)` returns None today.
Verify that first (workflow §16: reproduce, quote the behavior).

## Phase E1 — anchor data collection (the live hypothesis needs data to exist)

The trigger evaluates two consensus definitions separately, and the live
hypothesis is the Pinnacle sharp anchor (`consensus_anchor`) — currently
`enabled: false`, so no anchor rows are being collected and the anchor half of
any future regrade is unrunnable. A regrade run today could only re-test the
already-falsified soft median.

- Implement anchor fetching in the scan path: when `consensus_anchor.enabled`,
  `cmd_scan` additionally fetches the anchor book (h2h ONLY — config comment
  is binding: full anchor = 540 credits/mo > the 500 budget; rule 2) and
  persists per-book rows tagged so regrade can select them.
- Budget arithmetic goes in the commit message: current scan cadence ×
  credits/call + anchor cost, shown < 500/mo with margin. The `CreditsLow`
  abort (oddsapi.py) already backstops it.
- Flipping `consensus_anchor.enabled: true` is a config change with a dated
  line in PREREGISTRATION.md's amendment log ("anchor collection started
  YYYY-MM-DD; no rule changed — data collection only"). Data collection is not
  a rule change, but date it anyway; the regrade must later exclude pre-anchor
  days from the anchor comparison.
- Tests: fixture-driven parse of the anchor response shape; assert h2h-only
  request construction; assert no anchor call when disabled.

## Phase E2 — `arb regrade` (read-only scorer)

Implement `cmd_regrade`. Read-only against the DB (or `--regrade-db PATH`
copy); writes nothing but an annotation row summarizing the run (kind
`regrade`, detail = the JSON verdict). Post-cutover data only — hardcode the
cutover date in config (rule 4), not in code.

For every settled event with both stored consensus and venue quotes:

1. **Paired Brier, two clocks.** Per-event Brier difference (consensus −
   venue mid), computed both **as-traded** (last consensus before the venue
   quote it would have flagged — the trading question) and **same-timestamp**
   (consensus vs venue mid at scan time — the science question). Cluster
   bootstrap over `event_key`, 95% CI on the mean paired difference. The
   archived verdict compared 8h-stale consensus to 60s-fresh mids; the two
   clocks exist to separate staleness artifact from real signal.
2. **EV-subset win rate.** Over events where consensus and venue disagree
   past the fee-adjusted threshold (`ev.e_min` — the booking floor, not the
   display gate), the venue side's win rate with a 95% cluster-bootstrap CI.
3. **Two consensus definitions, never pooled:** soft-book median (from
   `consensus` rows) and Pinnacle anchor (from E1 rows, anchor-tagged) —
   separate tables, separate CIs.
4. Sample sizes and the settled-outcome source (mlbstats) printed with every
   number; VOID/pushes excluded from win-rate denominators (§11 convention).

- Statistics live in a new small module (e.g. `arb/regrade.py`, <300 lines);
  bootstrap uses stdlib `random` — floats are legal here (rule 5 carve-out:
  statistical root-finding/resampling), but every reported money/prob number
  re-quantizes q6.
- Failing tests first, from synthetic fixtures with known answers: a fixture
  where consensus is strictly better must PASS condition 1; one where it's
  equal must FAIL (CI includes 0); win-rate CI must widen when all rows share
  one event_key (clustering actually applied — assert the CI differs from the
  iid case).

## Phase E3 — the gate evaluator and the flip mechanics

- `arb ev-gate`: runs regrade, evaluates the three trigger conditions, prints
  PASS/FAIL per condition with its CI, and appends an annotation (kind
  `ev-gate`, detail = verdict JSON). It NEVER edits config.
- The flip, when (and only when) all conditions PASS: a human (or you, in a
  dedicated commit the owner approves) appends a dated amendment to
  PREREGISTRATION.md quoting the ev-gate verdict annotation id, and sets
  `ev_enabled: true` in the same commit. Add a startup consistency check in
  `arb/config.py`: `ev_enabled: true` without an `ev-gate` PASS annotation in
  the DB (or an explicit `ev_override_amendment: <date>` config key naming a
  human amendment) is a ConfigError — the switch physically requires either a
  passing gate or a dated human override, never a silent edit.
- Tests: config validation both ways; ev-gate verdict JSON golden test.

## Phase E4 — forward-test guardrails (arm these BEFORE the flip can happen)

If the gate passes, EV bets start booking through the existing tracker path
(no tracker changes — `classify()` already returns clean when `ev_enabled`).
Pre-registration imposes forward-test conditions that must be armed in
advance:

- **Kill rule:** 10 consecutive losing EV forward-test bets halts the test.
  Implement as a check in the settle cycle: on each EV settlement, compute the
  current consecutive-loss run (read-time, from bet_events); at 10, write a
  `kill-rule` annotation and flip nothing — the annotation plus the E3
  startup check pattern (a `kill-rule` annotation newer than the last
  amendment forces `ev_enabled` to be treated as false at startup) halts
  booking without a config edit racing the supervisor. Loss-run length is
  config (rule 4). A kill is halt-and-review (the ~20% false-kill note is
  pre-registered), not proof of negative edge.
- **CLV machinery:** go-live per segment needs mean CLV > 0 over the
  pre-registered settled-bet sample. Primary benchmark: the venue's own
  closing mid (already in `quotes` — derive at read time in metrics.py).
  Secondary: last consensus within `clv_window_mins` of start; on re-open the
  supervisor adds the per-event T−30m consensus fetch (credit-budgeted, only
  while `ev_enabled`). Bets with no in-window consensus close are excluded
  from that benchmark but counted in coverage. Report CLV in the daily EV
  section from day one of the forward test.
- **Headline separation decision (owner input, flag it, don't decide it):**
  once `ev_enabled`, clean EV rows enter the headline filter
  (trusted+sports+pregame+clean) immediately, while the CLV go-live gate is
  still unevaluated. If the owner wants forward-test EV segregated from the
  headline until CLV passes, that is a pre-registration amendment changing
  the headline definition — write the amendment text as a proposal in the PR
  description, do NOT implement it unilaterally. Until decided, the daily
  report MUST break EV out as its own line so the blend is visible.
- Tests: kill-rule run-length from synthetic bet_events; CLV derivation from
  fixture quotes; startup treats kill-annotation as frozen.

# Part B — weighted-ARB activation (edge × depth; PREREGISTRATION.md "Capacity")

The gate: `arb_size_by_ladder` flips only after (1) ≥ 50% of feasible arbs
carry real multi-level ladders on every leg and (2) the capacity report shows
genuine edge-limited (not depth-fantasy) arbs. The sizing math already exists
(`arb/sizing.py`: VWAP, q*, depth haircuts; `arb/capacity.py`: the
INSUFFICIENT_DEPTH_DATA gate). The blocker is structural:

**Kalshi quotes never carry ladders** — `arb/venues/kalshi.py:81` sets
`ladder_json=None` (the markets endpoint has no ladder; `fetch_orderbook`
exists at kalshi.py:120 and is called by nothing). Any position with a Kalshi
leg can never satisfy "multi-level ladders on every leg", so condition (1) is
unreachable for the dominant venue pair. Reproduce first: run
`capacity_frame` on a live-DB copy and quote the `ladder_fraction`.

## Phase W1 — Kalshi ladder capture

- After detection, for each locked flag (ARB/XSPREAD) with a Kalshi leg,
  fetch that market's orderbook (`fetch_orderbook`, public GET, keyless) and
  attach the ladder to the in-memory Quote BEFORE `run_cycle` persists quotes
  — enrichment happens between `detect()` and `insert_many("quotes", ...)`,
  so no row is ever updated (the store has no UPDATE path anyway).
- Scope it to flagged markets only: flags are ~15/poll; fetching every Kalshi
  book (~800/poll) would be a self-inflicted rate-limit incident (venue rate
  limits are dated facts — record the limit beside the adapter, §6).
- Conversion is subtle and MUST be fixture-tested before wiring (the rule-10
  pattern): `orderbook_fp` is per-side BID levels `[price_dollars,
  contracts]`; the YES ask ladder derives from NO bids as
  `(1 − no_bid_price, contracts)` sorted ascending, and vice versa (the
  docstring at kalshi.py:120 pins this, verified 2026-07-08). Record one real
  orderbook response into `tests/fixtures/` first; write the failing
  conversion test from it; only then implement.
- Emit ladders in the same JSON shape polymarket uses (parse_ladder already
  accepts it).
- Acceptance: on a copy of the live DB after one supervised cycle,
  `capacity_frame` ladder_fraction strictly increases; quote it in the commit.

## Phase W2 — operationalize "edge-limited, not depth-fantasy" (condition 2)

Condition (2) has no pinned measurement yet. Defining one after seeing the
data would be the forking-paths mistake, so: propose the operationalization
as a dated PREREGISTRATION.md amendment IN THIS COMMIT, before any gate
evaluation runs. Proposed definition (adjust wording with the owner, then
freeze): for each projected position, `sizing.arb_projection` reports which
constraint bound the size — `q*` (slippage/edge), ladder exhaustion (depth),
or the bankroll cap; the capacity report is "edge-limited" on a day when the
binding constraint is `q*` or bankroll for ≥ `capacity.edge_limited_min_frac`
(config, rule 4) of trusted+clean positions. Extend `arb_projection` to
return the binding constraint (it already computes all three terms).
- Tests: synthetic ladders where each constraint binds; assert the label.

## Phase W3 — the gate evaluator and the flip

- `arb capacity-gate`: evaluates (1) ladder_fraction ≥
  `execution.min_ladder_fraction` over a trailing window (window length in
  config) and (2) the W2 criterion; prints PASS/FAIL per condition; appends a
  `capacity-gate` annotation. Never edits config.
- Same flip mechanics as E3: dated amendment + `arb_size_by_ladder: true` in
  one commit, plus a `config.py` startup check that the flag requires a
  passing `capacity-gate` annotation or a named override amendment.
- What the flip actually changes (and the ONLY thing it changes): the daily
  report's capacity section graduates from UNKNOWN to a weighted-ARB
  projection table — per position: edge_points, VWAP-priced executable size,
  binding constraint, projected P&L = edge × filled contracts. **The flat-$1
  headline does not change** (SPEC §"Why measurement-first": Kelly/weighted
  numbers are parallel projections only; there is no weighted headline).
  Guard that with a test: report output with the flag on must contain the
  projection table AND an unchanged headline number vs the flag-off run on
  the same fixture DB.

---

## Ordering and verification

- Part A and Part B are independent; within each, phases are strictly
  ordered. E1 first regardless — anchor data has a calendar cost (every week
  not collecting is a week the regrade can't use).
- Full `pytest -q` green per phase; `arb report --daily` unchanged except the
  EV breakout line (E4) and the capacity section when its gate passes (W3) —
  both called out in commit messages with before/after.
- Nothing in this prompt touches `eligibility.py`, the §7 vectors, fee code,
  or the tracker booking path. If an endpoint 404s while recording the W1 or
  E1 fixtures, STOP and report (rule 3).
- After both parts land: `arb ev-gate` and `arb capacity-gate` join the
  supervisor's weekly schedule (config times), so the day a gate passes is
  noticed, dated, and logged — not discovered by accident a month late.
