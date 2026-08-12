# Next phases — implementation instructions

Ordered by dependency. Every item obeys the CLAUDE.md hard rules: fixtures-first (record one
real response before parsing it), every threshold in `config.yaml`, `Decimal` for money,
`flat_bets` append-only, SPEC §8/§9 vectors exact. After each item: `pytest` green, then run
the acceptance command and eyeball the output.

Current state (2026-06-11): all three venues live; ARB-only mode (`consensus_enabled: false`);
scan/settle/daily-report scheduled in the daemon; tracker records ARB + DIVERGENCE(stake 0);
EV class dormant until the flag is flipped.

---

## 0. Re-enable EV bets (one line, whenever ready)

Set `consensus_enabled: true` in `config.yaml`, restart the daemon. Nothing else: the scan
cadence, EV detection gate, and settlement already key off this flag.
Credit cost when on: scans 12/day + settle 2–4/day ≈ 450/mo (limit 500, hard abort < 50).

---

## 1. Full daily report (Phase 6 completion — highest value, no new data needed)

Everything derives from `flat_bets`; never store aggregates (SPEC §9: idempotent recompute).

**Files:** new `arb/daily.py` (~250 lines budget); `arb/tracker.py` keeps only poll recording;
`write_daily_report` moves to `daily.py` and re-exports.

**Functions:**
```python
def daily_rows(store: Store, day: date) -> list[sqlite3.Row]
    # all flat_bets rows with placed_at on `day` (America/New_York boundary, not UTC —
    # convert before substr-ing; current code compares UTC date, fix while here)

def summarize(rows) -> dict
    # per (kind × fill_assumption[optimistic|confirmed] × trusted[1|0]):
    #   bets, staked, settled_pnl, pending_count, win_rate, roi
    # breakdowns: by venue-pair (from legs_json), by sport, by category,
    #   by EV edge bucket: 0–1%, 1–2%, 2–5%, >5%   (SPEC §9 — the threshold-tuning table)
    # extremes: biggest win, biggest loss, largest pending edge
    # honesty: flagged edge (edge column) vs realized edge (settled pnl / stake) per kind
```

**Headline number** = trusted=1 AND live=0 AND category='sports' only; everything else shown
below it, never mixed (SPEC §9 trust split). Mark restated days with `*` when any SETTLED row's
`settled_at` date ≠ its `placed_at` date.

**Left on the table:** filtered flags are currently only printed, not persisted — add an
`opportunities` row with `status='FILTERED'` and a note naming the filter that killed it
(change `apply_opportunity_filters` to return the reason). The daily report then counts
yesterday's filtered flags + summed edge from the `opportunities` table.

**CSV columns (one row per bet):** id, fingerprint, kind, sport, category, venue_pair, trusted,
live, edge, stake_usd, status, result, pnl_optimistic, pnl_confirmed, placed_at, settled_at.

**Tests:** build a store with one ARB (won, locked), one EV settled WON, one EV settled LOST,
one EV pending, one DIVERGENCE; assert every aggregate by hand. Win-rate denominator is
settled bets only.

**Acceptance:** `python -m arb report --daily` after a live overnight run; numbers must
reconcile with `SELECT SUM(CAST(pnl_optimistic AS REAL)) FROM flat_bets WHERE status='SETTLED'`.

---

## 2. Trusted pairs (process + config only — unlocks real ARB fills)

No code. For each venue pair + sport you want headline-trusted:

1. Read both rulebooks for the market type. Kalshi: the series contract PDF
   (`contract_url` field in `data/kalshi_series.json` source response). Polymarket: the
   market's `description` field (resolution criteria). SX: outcomeVoidName semantics at
   docs.sx.bet → Betting Rules.
2. Verify all of: extra innings/OT counted the same; postponement/void rules the same;
   totals settle on the same statistic; same settlement source or equivalent.
3. Only then append to `config.yaml`, e.g. `trusted_pairs: ["kalshi+polymarket:baseball_mlb"]`.
4. SX single-venue legs untrust via the same mechanism: add `"sx+sx:baseball_mlb"` only after
   confirming `NO_GAME_OR_EVEN` void rules are acceptable for that sport.

Known trap (SPEC §12): Kalshi MLB counts the game's final score incl. extra innings;
postponed games void differently per venue — this is exactly why the default is REVIEW.

---

## 3. Break-window re-scans (`scan_breaks`, config already present)

**Goal:** during NBA halftime / soccer halftime, quotes are captured with `live=true`,
detected, and reported in their own section — never in headline numbers (SPEC §1, §12).

**Files:** `arb/cli.py` (`split_pregame` → `split_by_timing`), `arb/report.py` (live section).

```python
def split_by_timing(quotes, break_windows: dict[str, list[int]], now) -> tuple[list, list, list]
    # returns (pregame, in_break, in_play)
    # in_break: sport has a window [offset_min, length_min] and
    #   start + offset <= now <= start + offset + length  → live=True, DETECTED
    # in_play: started, not in window → live=True, NOT detected (current behavior)
```

Detection runs twice: once on pregame (headline pipeline, unchanged) and once on in_break
quotes alone; in-break opportunities get note `in-break` and are printed under a separate
"LIVE (BREAK)" table. Tracker rows from them carry live=1 (already excluded from headline).

**Trap:** never blend pregame and in_break quotes in one detection pass — a halftime price
against a pregame price is a fake arb by construction.

**Tests:** synthetic NBA quote at start+50min with window [48,15] → in_break; at start+70min →
in_play; soccer at start+44min → in_play (window starts at 45).

**Acceptance:** run the daemon through an NBA Finals halftime; `SELECT COUNT(*) FROM quotes
WHERE live=1` grows during the window, and no live row appears in the headline daily section.

---

## 4. Non-sports category (politics/econ/crypto — SPEC §1, §5.4)

**Manual-only matching. Never auto-match** (settlement wording risk is the whole danger).

**Files:** new `arb/nonsports.py`; `data/nonsports_map.yaml`; `logs/nonsports_candidates.log`.

**Map schema (`data/nonsports_map.yaml`, hand-edited):**
```yaml
- id: fed-cut-2026-06          # becomes event_key "nonsports:fed-cut-2026-06"
  kalshi_ticker: KXFEDDECISION-26JUN
  polymarket_market_id: "0x..."   # market id or token ids
  outcome_map: { "Yes": "YES", "No": "NO" }   # per-venue label → canonical
  close_by: 2026-06-18
```

**Candidate suggestion:** during watch, fuzzy-compare Kalshi non-sports titles vs Polymarket
questions with `rapidfuzz.token_set_ratio >= nonsports_candidate_threshold` (new config key,
default 85) and append one line per pair to `logs/nonsports_candidates.log` — suggestion only,
never promoted automatically.

**Pipeline rules:** `category='nonsports'`; horizon filter `nonsports_max_days` (config, 7);
every opportunity REVIEW unless the pair id is whitelisted; settlement **only** via
`python -m arb settle --manual nonsports:<id> <WINNING_LABEL>`; own report section, never in
headline.

**Tests:** map fixture with one mapped event on two venues → one event_key, category
nonsports, REVIEW; unmapped lookalike titles → candidates log line, no quotes.

---

## 5. Odds API key discovery (consensus for soccer/tennis/MMA — optional, budget-driven)

`GET /v4/sports/` is **free** (SPEC §3.4). Cache to `data/oddsapi_sports.json`.

Add config mapping (only keys listed here are ever scanned — rule 4):
```yaml
odds_sport_keys:
  baseball_mlb: [baseball_mlb]
  basketball_nba: [basketball_nba]
  soccer: [soccer_epl, soccer_uefa_champs_league]   # each key = 2 credits/scan
```
Credit math before enabling anything: `2 × n_keys × 3 scans/day × 30 ≈ credits/mo ≤ 450`.
With the current 2 keys there is no headroom for more than ~1 extra key; if tight, drop
`totals` from `MARKETS` in `arb/venues/oddsapi.py` (SPEC §3.4 sanctions exactly this fallback).

Consensus/settlement then iterate odds keys per sport; event_key building is unchanged
(scores/odds both carry full team names).

---

## 6. Divergence activation (stat-arb — only after weeks of data)

Do not build until the daily report (item 1) shows divergence spreads mean-revert. Then:
- new config: `divergence: { stake_usd: 1.00, entry_threshold: 0.05, exit_threshold: 0.01 }`
- tracker: DIVERGENCE rows get `stake_usd` from config instead of "0"; a position is
  long cheap venue / short rich venue *notionally* (still paper); close row appended when
  spread < exit_threshold; pnl = entry spread − exit spread per $1.
- Backtest first: replay from the `quotes` table (this is why rule 7 persists everything).

---

## 7. Parlays (Phase 7 — only after 1–6)

**Sources:** Kalshi MVE markets (appear in the normal `/markets` feed; legs in
`mve_selected_legs` — record a real fixture first); SX Daily Parlays (sportId 25);
Polymarket CAOC. EV signals only; always REVIEW; never auto-filled; own ledger
(`category='parlay'` rows, stake 0 until explicitly enabled via `parlay.enabled`).

**Math (`arb/parlay.py`), mandatory vectors:**
```python
def frechet_bounds(leg_probs: list[Decimal]) -> tuple[Decimal, Decimal]
    # lower = max(0, Σp − (n−1)); upper = min(p)
def independence_fair(leg_probs: list[Decimal]) -> Decimal   # Πp, cross-game only
```
Vector (SPEC §11): legs (0.60, 0.55) → bounds [0.15, 0.55], fair 0.33;
ask 0.12 → class 1 (violates lower bound, underpriced under ANY correlation);
ask 0.25 → class 2 (below independence fair by ≥ `parlay.divergence_threshold`).
Same-game legs: never use independence; annotate direction only (positive correlation ⇒
fair > product). Apply `min_liquidity_usd` strictly — observed Kalshi MVE books at zero.

---

## Review fixes deferred from 2026-06-11 (implemented that day: depth floor pre-detection,
## match sanity gates, sequential-fill edge in flash verify, annotations table, report footer)

- **Architecture inversion (review #2):** sweep becomes a nominator only (anything within
  ~2% of arb territory); focus polls nominated games every 5–15s and is the primary arb
  detector. Sweep-detected arbs get tagged `slow-detect`. Do after the staking test.
- **Whitelist TTL (review #4):** when the first venue-pair is whitelisted, `trusted_pairs`
  becomes a structured block: rulebook excerpt + URL + date checked + reviewer + 60–90 day
  expiry; expired pairs revert to REVIEW; any settled bet showing venues resolving
  differently auto-revokes the pair.
- **Settlement reconciliation (review #5):** poll Kalshi/Polymarket actual market
  resolutions at T+1, write agree/disagree rows; disagreements append corrected-outcome
  rows + increment a settler-error counter; postponed/suspended games skip auto-settle.
- **Match-rate residue (review #7):** per sweep, persist markets fetched/matched/unmatched
  per venue with raw titles; daily match-rate trend; >5-point day-over-day drop alerts;
  unmatched titles feed an alias-suggestion queue.
- **Full cost + capital model (review #8):** Polymarket gas/relayer/redemption per-contract
  estimate; per-bet dollars-locked × lock-duration; report return on capital-days; edges
  below fees + ~5% annualized capital charge graded `uneconomic`.
- **Doubleheader game numbers (review #6 tail):** use venue game-number fields where
  exposed; refuse to match a team-pair with two same-day listings until disambiguated.
- **Threshold fitting (review #10 tail):** add skew-seconds and edge columns to recorded
  flags; after ~4 weeks plot VERIFIED% against both and re-fit the 60s/4% gates.
- **Settlement addendum report (review #3 tail):** after each 09:30 settle, auto-append
  realized P&L to any named test window's report.

## Review-2 fixes deferred from 2026-06-11 (~22:20Z; implemented that hour: PREREGISTRATION.md,
## suspect-match quarantine table, seq edge both orderings + lag, ARB provisional→settled rows,
## dated fee facts with stale warnings, stop-flag files)

- **#3 core — deterministic dedup:** `opportunity_id = hash(sport, game_key, market, line,
  venue_pair)`; UNIQUE(opportunity_id, active_window) with insert-or-ignore; re-flag only
  after VANISHED/close + 30-min cooldown; report takes earliest verdict per bet.
  Do immediately AFTER the staking test window closes (schema semantics must not change
  mid-measurement).
- **#2 — settlement inversion:** venue resolution APIs (keyless) become primary settlement;
  Odds API scores demoted to cross-check; disagreement increments settler_error; split
  credit floors (scans abort <100, settlement may spend to 0); report settlement coverage.
- **#4 — launchd supervision + heartbeats:** KeepAlive with backoff, crash counter, per-cycle
  heartbeat rows, expected-vs-actual cycles + uptime% atop the daily report. Keep the
  environment constant (no cloud move mid-experiment).
- **#9 — request/response timestamps:** per-quote request_sent_at/response_received_at;
  worst-case cross-venue skew bound (earlier sent vs later received); NTP offset check at
  boot. Record everything; the grade carries the uncertainty.
- **#7 — schedule-aware quote-count alerting:** zero quotes while games are scheduled, or
  >50% drop vs same-day expectation → ERROR line + red report header row.
- **#8 tail — whitelist entries as dated facts:** expires_on (90d), auto-revoke on observed
  settlement mismatch, revocation affects future flags only (past bets get annotations).
- **#6 tail — quarantine review workflow:** daily report section listing unclassified
  suspect_matches; classification updates the row's classification column (alias-bug /
  real-dislocation / mismatch) and alias-bug ones feed the alias table.

## Review-3 fixes (2026-06-12; implemented: #1 RESEARCH class for unverifiable legs,
## #6 capturable lower bound on every arb, #9 lead-lag analysis over the archive)

First lead-lag result (move>=0.01, lag<=300s): kalshi->polymarket median 42s (n=2096) vs
polymarket->kalshi median 209s (n=1588) — asymmetry suggests **Kalshi leads Polymarket**.
CONFOUND to fix before trusting it: Polymarket has 9x Kalshi's move count, so "nearest
following move" is base-rate biased toward fast Polymarket follows. Next: null-model
correction (shuffle move times, compare observed lead vs random-baseline lead).

Still queued from review #3:
- **#3 — general single-venue scanner:** extend `political` to all Kalshi binaries +
  Polymarket negative-risk multi-outcome (sum of YES asks < 1), gated STRICTLY on the
  venue's own exhaustive-outcomes metadata (never self-enumeration). These flags are
  gradeable by definition → fastest path to the n=100 threshold.
- **#2 — fee-aware market ranking:** pre-rank by fee-adjusted headroom; Kalshi viable
  arbs concentrate at extreme P where 0.07*P*(1-P) shrinks. Pure budget reallocation.
- **#4 — movement-triggered focus + keyless websockets** (Polymarket CLOB, Kalshi) with
  per-venue token-bucket budgets; polling stays as WS-drop fallback.
- **#5 — hot decay schedule** (1s→10s→60s), concurrent-slot cap, priority by
  edge*verifiability; record poll interval per sample so lifetime stays interpretable.
- **#7 — widen funnel** (more sports in-season + single-venue scanner) to raise gradeable
  flags/day; kill criteria stay frozen.
- **#8 — CLV grading (repoint, high value):** record each venue's own last pre-game mid
  (keyless, $50 depth floor); grade every flag vs its venue close. Converges far faster
  than settlement P&L; answers "does flagging beat the close?" in ~2 weeks.
- **#9 next — lead-lag null correction + live targeting:** once de-confounded, quote the
  lagging venue the instant the leader moves.
- **#10 — schedule-aware cadence:** ramp focus/verify around start/lineup/close; 15-min
  heartbeat otherwise (keeps single-venue scanner alive overnight).

## Operational backlog

- **launchd auto-start** (survive reboots): plist at `~/Library/LaunchAgents/com.arb.daemon.plist`
  with `ProgramArguments = [caffeinate, -i, <abs>/.venv/bin/python, -m, arb, daemon]`,
  `WorkingDirectory = <project>`, `KeepAlive = true`, env `PYTHONUNBUFFERED=1`;
  `launchctl load` it. Remove the shell-started daemon first (`pkill -f "arb daemon"`).
- **Log rotation:** `logs/daemon.log` grows ~1 MB/day; rotate weekly or pipe through
  `split`/`newsyslog`.
- **Kalshi series cache refresh:** re-run the §3.1 discovery monthly or when a sport's
  `kalshi: 0 quotes` persists for a day (new season → new series tickers).
- **DB backup:** `data/arb.sqlite` is the entire record; copy it daily
  (`sqlite3 data/arb.sqlite ".backup data/backup-$(date +%F).sqlite"`).
