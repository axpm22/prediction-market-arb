---
title: "ArbitrageModel — Complete Rebuild Specification (v3.5)"
subtitle: "Kalshi / Polymarket / SX Bet paper-trading arbitrage scanner with flat-stake measurement ledger"
date: "July 7, 2026"
geometry: margin=2.2cm
fontsize: 10pt
colorlinks: true
toc: true
toc-depth: 2
numbersections: true
---

\newpage

# v3.5 changelog (2026-07-07)

v3.5 integrates the production-readiness pass (REBUILD_SPEC_V3.5_PRODUCTION_ADDENDUM.md — the capital-preservation layer; the addendum is the authoritative statement, this doc wires in the binding parts). **Taxonomy (RULE 1):** "arbitrage" is same-venue only; cross-venue positions become kind `XSPREAD`, never called arb anywhere (§2, §7.1, §8.4, §12, config `kind_taxonomy`). **Settlement risk is priced, not just gated:** XSPREAD eligibility computes `net_spread_edge = gross − fees − (3/n)·LGM` from the pair's Resolution Equivalence Certificate (rule of three over n mismatch-free positions, expiring on rulebook edits); at the §18.2 gate's n=100 that is 3 points — the whitelist is necessary, the arithmetic is binding, and positive net requires n ≥ 600 (§2, §12, §18.2). **Structural auto-book disable:** the floor `max(2F, 0.5%)` exceeds the REVIEW ceiling `F + 0.5%` for every cross-venue fee total above 0.5% — the XSPREAD auto-book window is the empty set at all non-tail prices; codified as `deployment.xspread_autobook: false` (config, addendum §2). **Sizing layer:** projection fills price at ladder VWAP; caps `min(quarter-Kelly, depth/5, q*)` with slippage ≤ 20% of edge; portfolio caps per event/sport/day-totals; deployment requires annualized edge > 20% over a dated `risk_free_rate` and a passing sport-day-block Monte Carlo ruin report (§13.4, config). **Execution:** the two-leg state machine with LEG_RISK resize-or-unwind lives in the addendum §4; the verify lane doubles as its paper fill oracle (§5 layout). Five new independent findings recorded in the addendum, including the rule-of-three insufficiency of the 100-position gate and the NegRiskAdapter conversion effect on NO-basket lockup. **Dry-run patch (same day):** logical test runs against this version found the exhaustiveness proof's tolerance admitted worst-case-negative baskets into the headline (a 1.9% hidden tail inside the 2% window beats a 0.5pt edge floor: EV −0.41pts) — fixed by the A1 coupling invariant `min_edge > (1−mids_sum_min)/mids_sum_min` with new defaults (0.006, 0.995), loader-enforced, plus A2: the proof is invalid when summed half-spreads exceed the window slack (§8.4, config `negrisk`). **Error-hunt loop (same day, run to termination):** pass 1 (inter-edit consistency) found the taxonomy rename had orphaned XSPREAD mechanics — no bet-creation rule, no registered settler, a §12 startup error by the spec's own definition — fixed by the L1 mechanics-binding clause (§2), plus two minors (n=0 certificate division pinned ineligible, §12; `max_half_spread_sum` bound to the window slack by a loader assertion, §8.4). Pass 2 re-derived every v3.5-new formula (empty-window condition nonempty iff 0 < F < 0.5%; rule of three; A1 breakeven m = p·S/(1−p); Kelly equivalence; MDE; debiased-ECE floor) — zero errors. Pass 3 (guard interplay: certificate expiry × trusted_pairs, L1 × tripwire/invariants, booking rule × frozen EV, REVIEW-settle path for XSPREAD rows) — zero CRITICAL/MAJOR. Pass 4 (full post-fix re-sweep) — zero CRITICAL/MAJOR; loop terminated. The two minors initially accepted were subsequently fixed on request: the annualized-edge formula now takes the geometric excess `(1+e)^(365/d)/(1+rf) − 1` (both docs), and the XSPREAD naming is propagated through §2 item 5 and the §18 pre-registration prose. **Buy/sell layer (same day):** §10.8 adds the exit engine — the simulated SELL side: three pre-registered policies (hold / take-profit at `capture_fraction` of flagged edge / trailing stop off peak unrealized) computed read-time from stored quotes with sells priced at bid − taker fee, a Phase-6 exit vector ($0.087822), peak-captured-% reporting, and a promotion gate (n ≥ 100, cluster CI, replication month); "sell at maximum profit" is stated honestly as unimplementable ex ante, with P1/P2 as the measurable approximations. §16 gains an operator quickstart: exact build/run/stop commands, BUY/SELL semantics, and the explicit statement that live order placement stays forbidden (rule 1) behind the deployment gates.

# v3.4 changelog (2026-07-07)

v3.4 applies the fifth-pass adversarial audit (34 findings: 2 critical, 9 major, 23 minor; 29 applied, 5 deliberately deferred — see review record). **Critical:** the markout P&L sign is corrected — the v3.2 ("enter at the mark") and v3.3 ("−drift") forms both *rewarded* picked-off fills; v3.4 pins one-sided adverse drift `pnl_markout = pnl_optimistic + Σ min(0, mid_h − mid_0)` with the decision rule restated as `mean(D_2m) < −0.5 × mean(edge_points)`, pregame-valid marks only (§10.5). The Quote model gains `side{YES|NO}`: complement-mapping NO→opposite outcome is legal only for strict two-outcome binaries — never soccer (NO(A) = {B ∪ draw}) and never integer-line totals (push) — closing the data-model path that silently rebuilt the draw-hole fake arb; `side` propagates to fingerprints, position keys, and the quotes schema (§7.1, §9). **Major:** `edge_points` pinned as probability points per $1 payout, ROI display-only (§7.1); the Pinnacle anchor is h2h-only — full anchor = 540 credits/mo > the 500 budget (§6.4, config); the negRisk proof gains condition (c): a two-sided book on every listed outcome (§8.4, config); the win-rate CI is cluster-bootstrap, not "clustered Wilson" (§18.3); ECE gating moves to a debiased estimator (raw noise floor ≈ 0.06 at n=200/5 bins) at 0.05 (config); a power disclosure states the MDE (≈0.65 win rate at n_eff≈90) with a [NEEDS INPUT] pre-registration decision (§18.5); confirmations and markout marks are valid pregame only (§10.3, §10.5); resolvers gain `PARTIAL(payout_fraction)` for UMA fractional settlements, `pnl = stake × (fraction/cost − 1)` (§11); stale-consensus first sightings write `left_on_table` instead of booking a life-quarantined bet, and `scan_times` retime to cover modal MLB starts (§13.1, config). **Minor:** dispersion = IQR; quality_flag enum pinned; locked_pnl = pnl_optimistic; micro/live are category tags, not lanes; bet_events payload schemas pinned; quantization at store/compare boundaries; micro e_min interpolation domain pinned; DB estimate corrected to 4–8 GB/mo; NBA break offset flagged placeholder; USD:USDC basis noted in projections; taker_price_buffer applies to thresholds, never stored costs; headline labeled gross-of-carry; nulls scoped cadence-conditional; re-open regrade restricted to post-cutover data; day-level correlation acknowledged; venue rate limits become dated facts; same-volume backup dest warns; Phase-6 acceptance de-flaked; invariant 6 (unique settled event) added; left_on_table gains venue_pair.

# v3.3 changelog (2026-07-07)

v3.3 applies the fourth-pass review (17 findings: 2 critical, 7 major, 8 minor). **Critical:** the negRisk exhaustiveness requirement gains a mechanism — venue structural flag AND a mids-sum window; failing baskets are REVIEW `unproven-exhaustive`, closing the short-the-missing-outcome hole in the one headline-eligible arb class (§2, §7.2, §8.4, §12, §15, §18.4). The append-only rule gains its mechanism: `flat_bets` holds entry-time facts only, a new append-only `bet_events` table carries confirmation/close/settlement/reconciliation, `metrics.py` derives pnl_confirmed/result/settled_at at read time, and the CI grep extends to bet_events (rule 9, §9, §10.3, §11.1). **Major:** the political lane never scans Kalshi — `no_ask = 1 − yes_bid` makes intra-market YES+NO<1 a crossed-book artifact (§6.1, §12); `pnl_markout` is redefined as drift-adjusted optimistic so all three P&L columns share one entry basis (§10.5); `consensus_books` stores per-book de-vig so the n≥200 bake-off re-run is actually runnable from stored data (§7.2, §9); date±1 bridged matches re-key to a canonical event_key with an alias map (§8.2); Polymarket book fetching must use a verified batch endpoint or a budgeted token set with reported coverage (§6.2); `arb_stale_warn` auto-REVIEW extends to every locked kind, not just cross-venue ARB (§8.4, config); maker economics get dated per-venue rates — Kalshi makers pay 25% of taker, never zero (§2, §6.5, config). **Minor:** zombie-guard volume fields pinned per venue (§8.4); consensus outlier drop disabled below 3 books, n=2 rows tagged `thin-consensus` (§7.2); the §2 example notes its own 4.17% would trip the stale warn; the weighted composite score is dropped in favor of the independent hard gates (config `scoring`); edge/velocity units clarified as probability fractions and the velocity constants marked placeholders for the lead-lag lane to re-estimate (config `execution`); tape confirmation carries the displacement caveat (§10.5); `max_child_runtime_secs` becomes a per-process map (§13.2, config).

# v3.2 changelog (2026-07-06)

v3.2 folds in the second- and third-pass reviews (SPEC_V3.1_REVIEW_ADDENDUM.md, SPEC_V3.1_REVIEW_FINAL_SWEEP.md; 59 findings). Detection: covering-set proof kills the soccer-draw fake-arb class (D1); zombie-volume guard (ME3); ARB ROI above `arb_stale_warn` books REVIEW automatically (M6); `line` is never float in keys (D2). Statistics: the EV re-open trigger now requires a paired bootstrap and — critically — a same-timestamp Brier comparison, because the archived "consensus is worse" verdict compared an 8h-stale consensus against 60s-fresh venue mids and may be a recency artifact (S1/M1); all CIs cluster by event_key; positives must replicate in a second out-of-sample month (S2/S3); CLV benchmarks the venue's own closing mid first (X3). Ledger honesty: missed confirmed fills book $0 (D4); absorbed cross-pair sightings are recorded so per-pair populations stay honest (ME1); reports read stored lanes, never re-derive eligibility (SC5); VOID and `completed:true` settlement conventions pinned (M3/M7). Schema: generalized annotations, poll_venues, markout offsets, venue_pair self-convention (SC1–SC4). New instrumentation: tape-confirmed fills from public trade prints (X1); monthly canary bets through the live pipeline (X6); independent SQL recompute of the headline (X7); weekly live shape-checks (O8); daily DB backup + committed daily reports (O3/OP10); per-venue fetch success by hour (OP11). New surface: Kalshi event_ticker negRisk groups (X4); lead-lag study lane (X5); AI-matcher recall bootstrapped on sports ground truth (ME4). Ops: SQLite write ownership + busy_timeout + indexes (O1/O2); strict config (O5); supervisor owns the clock, `schedule` dropped (O6); key redaction in logs (O4); pmset/lid-close, NTP alarm, log rotation (OP1–OP3); SX settled-endpoint verification gate (OP4); UMA dispute-window resolver semantics (OP5); missing keys `consensus_outlier_points`/`push_pricing` added (M2); golf removed, break_windows annotated, orphan keys bound (OP8/OP9/OP12).

# v3.1 changelog (2026-07-06)

v3.1 applies the findings of SPEC_V3_REVIEW.md. Corrections: the instant-settle tripwire and ledger invariant 3 are scoped to resolution-settled kinds so they no longer void the ARB class (A1/A2); the true-arb ROI vector is 0.037350 with ROUND_HALF_EVEN pinned globally (A3); paper-ledger fees use the continuous curve while `ceil_to_cent` is execution-only (A4); the consensus table gains market_type+line (A5); `position_key` is defined per kind, including ARB and NEGRISK (A6); micro is explicitly subordinate to `ev_enabled` (A7); the tracker fixture states its test config (A8); the Phase 6 cross-reference is fixed (A9). Hardening: event_key date pinned to America/New_York (B1); a venue outage can no longer mass-close bet lifetimes (B2); concurrent venue fetches with timeout/backoff are mandatory (B3); markouts are mid-basis with per-horizon sample tolerance and coverage reporting (B4/B5); the CLV closing mid is defined and fetchable (B6); the tripwire blacklist has a table (B7); left_on_table is reconstructable (B8); single-venue kinds have explicit trust semantics (B9); orphaned config keys are bound or removed (B10). Improvements: the de-vig bake-off is a coin-flip default with a pre-registered re-run (C1); a Pinnacle sharp-anchor consensus experiment replaces re-testing the falsified soft median (C2); negRisk scans the NO side (C3); MLB doubleheaders key on gamePk (C4); the kill rule's false-kill rate is pre-registered (C5); scores credit cost is verify-first (C6); DB growth estimate corrected (C7); NFL re-enable is dated (C8); the Polymarket WebSocket leads Phase 8 (C9); inter-venue skew joins cadence honesty (C10).

# Executive summary

This document is a complete, self-contained specification for rebuilding the ArbitrageModel project from scratch. It is written so that a Claude Code instance running Opus 4.8 (or a weaker model) can implement the entire system phase by phase, with every formula, endpoint, schema, threshold, test vector, and acceptance gate stated explicitly. It also folds in every shortcoming discovered during the first two builds (V1, June 2026; V2 hardening, July 2026) as design requirements, so the rebuilt system starts where the old one ended rather than repeating its mistakes.

**What the system is.** A Python CLI that watches the same event priced on more than one of Kalshi, Polymarket, and SX Bet; flags three opportunity classes (true arbitrage, divergence, +EV vs. de-vigged sportsbook consensus); and maintains an honest, append-only paper ledger answering one question: *"How much would I have made by today if I'd bet $1 on every flag?"* It is a **measurement instrument**, not a trading bot. It never places orders. Its most valuable outputs so far have been honest nulls.

**What the first builds established (the priors a rebuild inherits):**

- Riskless cross-venue sports arbitrage is a **mirage at retail paper scale**: 44 settled arbs totaled +$0.13 (~$0.003/bet); ~88% of flagged edges on the fast venue pair vanish on a 10-second re-quote; confirmed-fill P&L is indistinguishable from zero.
- The de-vigged sportsbook consensus is a **worse forecaster than the venues' own mids** (Brier 0.238 vs. 0.221/0.223; ECE 0.30 vs. ~0.07–0.11). The EV lane's sign was inverted; EV bet creation is frozen by a pre-registered decision rule.
- The instrument itself **works**: 617k quotes/day persisted, append-only ledger discipline caught and voided 100% of one day's fake P&L, activity gating kept API costs near zero, and pre-registration made the nulls citable.

**Why rebuild anyway.** The measurement harness is the asset. It cheaply disproves edges, and the classes that remain plausible (single-venue negRisk baskets with corrected fees, same-oracle BTC windows, maker-side counterfactuals, ladder-aware totals) all need exactly this instrument to be tested honestly. A rebuild with the fixes baked in produces clean evidence from day one instead of after three incident recoveries.

**How to use this document.** Read §4 (hard rules) and §16 (build phases) first. Implement phases in order; each has acceptance checks that must pass before the next phase starts. Every number lives in `config.yaml` (§15). Every claim about a venue API was verified live in June–July 2026; if an endpoint 404s, stop and report — do not invent alternatives.

# Strategy

## The three opportunity classes

Everything is priced in a single unit: **effective cost per $1 of payout** — the real executable order-book ask plus venue fees, expressed as an implied probability in `Decimal`.

**TRUE ARB / XSPREAD (v3.5 taxonomy).** Outcome-covering positions whose combined effective cost is under $1 per $1 of guaranteed payout. When every leg sits on **one venue** (proof-passing negRisk baskets, political intra-market, same-venue ladder crosses) the kind is `ARB` — the only constructs this spec may call arbitrage. When legs **span venues** the kind is `XSPREAD` (cross-platform spread): a position carrying settlement risk that must be priced (§12), never labeled "arb" in code, schema, reports, or prose. The rest of this paragraph describes the cross-venue XSPREAD case. If Kalshi sells YES at an effective 0.47 and Polymarket sells NO at an effective 0.49, buying both costs 0.96 and pays exactly 1.00 whichever way the event resolves: a locked 4.17% return — which would itself trip `arb_stale_warn` and book REVIEW, deliberately (§8.4, trap 1): at this project's scale, a return that size is near-certainly a stale quote or a settlement mismatch. The catch — and the project's central finding — is that the two quotes are rarely live simultaneously, depth is thin, and the two venues may settle the "same" event differently (extra innings, postponements, divergent oracles). Cross-venue XSPREADs are therefore **REVIEW-only by default**, forever, until a venue pair earns whitelisting through the settlement-consistency gate (§18) — and even a cleared gate does not make one bookable: net edge subtracts priced settlement risk `p_mismatch_upper × LGM` with `p_mismatch_upper = 3/n` (rule of three over n mismatch-free reconciled positions). At the §18.2 gate's n = 100 that is 3 points — larger than any observed spread; a positive net requires n ≥ 600 (addendum §1). The arithmetic, not the whitelist, is the binding constraint. **Mechanics binding (loop patch L1):** every mechanical rule this spec states for `ARB` applies identically to `XSPREAD` — bet creation ($1 split by the §7 stake formula, locked P&L = `pnl_optimistic`), locked-at-entry settlement, the §10.4 instant-settle tripwire exemption, invariant 3's pregame-at-placement check, the `'COVER'` position key, and the §12 registered-settlement-path requirement (XSPREAD's registered settler *is* the locked-at-entry path). The split changes naming, lane eligibility, and mismatch pricing — never mechanics. Without this clause the rename would leave XSPREAD with no bet-creation rule and no registered settler, which §12 defines as a startup error.

**DIVERGENCE.** A cross-venue spread on the same outcome that exceeds a threshold without locking profit. Logged with full quote data as future stat-arb fuel; never bet. (Note: the V2 audit queued divergence *bet-row* removal — ~1,600 no-op opportunity rows/day of ledger noise. In the rebuild, record divergences as opportunities/quotes only, never as ledger rows. See §10.1.)

**+EV.** Fee-adjusted venue cost below the de-vigged median of ≥3 sportsbooks ("consensus"). Every positive-EV quote is recorded regardless of size (the display threshold only gates what prints), which builds the data to tune thresholds later. **Critical inherited verdict:** on 68–877 settled archive samples, the consensus was a worse forecaster than venue mids and the venue side won only 47.9% of EV disagreements. The rebuild ships with `ev_enabled: false` and a frozen re-open trigger (§18.3). EV machinery is still built — it is the measurement instrument — but no EV dollar is paper-bet until the trigger fires.

**Secondary classes** (each single-venue or single-oracle, i.e. structurally safer than cross-venue XSPREAD):

- **NEGRISK** — single-venue exhaustive-outcome baskets (Polymarket negRisk events **and Kalshi `event_ticker` groups of mutually exclusive markets**), scanned on **both sides**: sum of YES asks (after fees) < 1 (buy every YES; exactly one pays), and sum of NO asks (after fees) < N−1 (buy every NO; exactly N−1 pay). Same structure, double the surface. Single venue, single oracle, no settlement mismatch: the cleanest arb construct in the system — **but only when the basket provably covers the outcome space.** A non-exhaustive group is a short position in every missing outcome: it prints steady fake "arb" profit until the tail hits. Every basket must pass the exhaustiveness proof (§8.4) before it is anything but REVIEW. V1 found all baskets at 1.005–1.011 (honest null), but that was measured with a wrong (double-charging) fee curve; the corrected quadratic fee lowers the bar, so the exhaustive re-scan is a priority experiment.
- **BTC window arb** — Kalshi vs. Polymarket short-horizon BTC price windows where both venues reference effectively the same oracle. Requires exact window end-time match (never interpolate) and a settlement path (V1 gap E3, fixed in §12).
- **PARLAY relative value** — Fréchet-bound violations and independence-baseline divergence on multi-leg markets. EV-signals only; a parlay can never be locked risk-free. Dormant until the core is green.
- **Non-sports (politics/econ/weather)** — fully separate category with manual-first matching and an AI-matcher (Haiku) second lane that is always REVIEW, always segregated, and never eligible for a trusted path without a human map entry.

## Why measurement-first is the strategy

The project's economics are asymmetric: a false "edge exists" costs real money if ever acted on; a true "no edge" costs electricity. So the design optimizes for cheap, honest disproof:

1. **Flat $1 per flag** is the headline, because sizing models multiply whatever bias exists in detection. Kelly/weighted numbers are parallel projections only.
2. **Optimistic vs. confirmed fills.** Every bet records the price at first sighting (optimistic) and at the second consecutive sighting (confirmed). The gap *is* the slippage estimate. If confirmed ≈ 0 while optimistic looks great, the strategy does not survive reality. (V1's confirmed column suffered survivorship bias — bets never seen twice were dropped rather than counted as misses — which is why the rebuild also adds markouts, §10.5.)
3. **Pre-registration.** Every hypothesis, gate, and n-threshold is frozen in `PREREGISTRATION.md` *before* its data accumulates. Changing a rule after data arrives is a dated, versioned amendment — never a silent edit. This is what makes a null citable.
4. **Append-only ledger.** Corrections are annotation rows or new rows, never UPDATEs. When bugs manufactured fake P&L (twice), the annotation mechanism voided it without destroying the evidence of what happened.
5. **Populations never pool.** The two XSPREAD venue-pairs (kalshi+polymarket ~88% vanish rate; polymarket+sx ~5% vanish rate — the latter an artifact of SX being slow, not of real edge) are pre-registered separately and never share a win rate, a gate, or a headline.

## Where edge might actually live (ranked by structural safety)

1. **NegRisk exhaustive baskets** (single venue, single oracle) with corrected quadratic fees and full pagination.
2. **Same-oracle BTC windows** (cross-venue but no settlement-rule risk if both oracles verifiably agree).
3. **Ladder-aware totals arb** — Kalshi quotes an exhaustive half-point totals ladder (2.5→12.5) that provably contains Polymarket/SX main lines in ~100% of multi-venue MLB totals events; detection must pair every strike, not one main line.
4. **Maker-side counterfactual** — both venues favor makers, but differently: Polymarket makers pay 0 (plus a taker-funded rebate program); Kalshi makers pay a **reduced fee, not zero** — 25% of taker, `0.0175 · C · P · (1−P)` (June 2026 schedule). Re-price every flag at the dated per-venue maker rates (§6.5) — never maker=0 on Kalshi, which overstates the flip fraction on every Kalshi leg — and log the flip fraction as an upper bound (measurement only; no fill-probability pretense).
5. **Venue-leads-books indicator** — the inverted EV signal, repurposed: venues beating the consensus is itself information.
6. **Lead–lag structure** — cross-correlate per-event mid *changes* between venues from the stored quotes: which venue moves first, by how many polls? Pure analysis of data already collected, the honest successor to the divergence class, and the quantitative test of the "SX is slow" claim.

Everything else — cross-venue moneyline arb as a P&L engine, whale copying, blind de-vig EV — is measured, was found wanting, and stays measured rather than believed.

# What worked and what didn't

This section is the distilled empirical record. A rebuilding model should treat every "didn't work" as a constraint to design around, not a bug to re-discover.

## What worked

| Component | Evidence |
|---|---|
| Quote persistence at scale | ~617,000 quotes/day on the fresh DB; ~2.95M in the V1 archive; WAL mode; poll-atomic writes; zero data loss attributable to the store |
| Append-only ledger + annotations | Caught and voided 100% of the 2026-07-02 incident's fake P&L (315 interleave duplicates, 48 rebook-loop rows, 20 in-play voids) without destroying evidence |
| Venue adapters, fixtures-first | All three venues parse recorded fixtures; SX maker-side inversion unit-tested; UFC tag migration caught by live verification |
| Activity gating | Dormant sports cost zero Odds API credits; monthly budget stayed under 500 with headroom (492 remaining at audit) |
| Flash verification + hot watchers | Measured arb lifetime directly: ~88% vanish on ~10s re-quote (kalshi+polymarket); lifetimes right-censoring found and fixed (raise cap to 900s, tag `censored`) |
| Pre-registration discipline | The EV freeze, the fee re-freeze, and the Stage-α rules all executed exactly as pre-committed — no post-hoc story-fitting |
| AI matcher negative-verdict caching | 8 verdicts, 0 positive, cached append-only at market level; budget guard held |
| Quality/quarantine layer | Collapsed the largest recorded EV "edge" from ~54,500% (empty-book artifact) to 18.2% clean — labels, never deletes |
| MLB Stats API as settlement oracle | Free, keyless, unlimited history; rescued bets outside the Odds API 3-day scores window |
| Supervisor + heartbeats (Phase D) | Structural fix for the "old process kept betting stale code" failure class; children one-shot, double-run refusal |

## What didn't work

| Finding | Numbers | Lesson baked into this spec |
|---|---|---|
| Cross-venue arb as P&L | 44 settled arbs = +$0.13 total; 88% vanish on re-quote (K+P pair) | Cross-venue is REVIEW-only forever by default; whitelist gate needs 100 reconciled positions at ≥99% settlement consistency (§18.2) |
| De-vigged EV consensus | Archive clean MLB EV: −$24.07 over 627 rows; consensus Brier 0.238 vs venue mids 0.221/0.223; EV-subset win rate 0.479 | Ship with `ev_enabled: false`; frozen re-open trigger; consensus kept as a venue-leads-books indicator (§18.3) |
| Confirmed-fill column as slippage proxy | Archive: −$19.13 optimistic vs +$40.64 confirmed — a survivorship artifact (never-seen-twice bets dropped), not slippage-beating | Confirmed=NULL counts as *missed*; markouts (+30s/+2m/+10m/+30m) added as the real adverse-selection measure (§10.5) |
| Settlement enforcement of REVIEW | 314/314 settled arbs were forbidden REVIEW rows booked into P&L (V1 bug A1) | One eligibility predicate consulted by *every* money-touching module (§12) |
| Reconciliation | 0 rows ever; only a Kalshi resolver existed while 100% of arbs had a Polymarket/SX leg | Per-venue resolvers required in Phase 5; P&L labeled `provisional` until reconciled (§11.2) |
| Capacity/sizing numbers | "Summed profit $72,209" was fantasy: 0 captured ladders, 4.5x row duplication, 99% REVIEW rows included | Capacity reports `INSUFFICIENT_DEPTH_DATA` until ≥50% of feasible arbs carry real multi-level ladders; dedup by position_key; trusted-only sums (§13.4) |
| Timing honesty | Watch cadence ~140s actual vs 60s configured; 85/314 settled arbs were placed *after* the event; Kalshi start-time fallback was `close_time` (days late) | Start-time truth hierarchy, in-play guards at detection AND settle, cadence printed in the daily report, instant-settle tripwire (§11.3, §13.1, §10.4) |
| Poll interleaving | Multiple loop kinds sharing absent-close logic created 315 duplicate bets in one day | Poll-kind ownership: only the poll kind that created a bet can close it (§10.3) |
| The dirty-tree failure class | Twice, a fix sat uncommitted while the old process kept betting | Supervisor + heartbeats + a hard rule: nothing restarts until pytest is green on a clean committed tree (§13.2) |
| Machine-sleep outages | Jul 4–6: three near-zero-data days; heartbeats stayed fresh while DNS failed | `caffeinate -i` / launchd KeepAlive in the documented start command; stale-data alarm (`data_stale_alarm_secs`) wired into status and daily report (§13.3) |
| In-place label mutations | A V1-era backfill script contained the tree's only `UPDATE flat_bets` and could NULL-clobber quarantine flags | Grep-test in CI: no `UPDATE flat_bets` / `DELETE FROM flat_bets` string may exist under `arb/` (§10.2) |
| Fee-model drift | Polymarket fee modeled linear while the venue had gone quadratic (double-charged mids by ~0.75pts — larger than most arb edges); negRisk used the Kalshi curve on Polymarket legs | Single `leg_fee_cost` implementation with per-venue `FEE_BASIS`; fees are dated facts with `source_url` + `confirmed_on` + staleness badge (§6.5) |
| Whale copying | Adverse selection; win rates above ~72% were zombie-order inflation | Class dropped from the rebuild (keep the debunk in the record) |
| Divergence as ledger rows | 177 zero-stake rows + ~1,600 no-op opportunities per day of pure noise | Divergences persist as quotes/opportunities only, never ledger rows |
| AI-matcher ground truth | Recall unmeasurable: zero confirmed-equivalent non-sports pairs ever existed | Ground-truth set is a prerequisite deliverable before any Haiku budget expansion (§18.5) |

## The one-paragraph verdict

The instrument works; the naive edges don't. A rebuild's purpose is not to find the arb the first build missed — it is to run a cleaner instrument over the classes that remain structurally plausible, produce trustworthy nulls or positives at pre-registered sample sizes, and keep costing nothing but electricity while doing so.



# Hard rules (never violate)

These rules bind every phase, every module, every prompt. They are restated here from the original CLAUDE.md, amended with the lessons of §3. A rebuilding model should copy this section into the new repo's `CLAUDE.md` verbatim.

1. **Paper trading only.** Never call authenticated or order-placement endpoints. Only public, unauthenticated GETs/POSTs — POST solely for read-only batch quote endpoints verified in §6 (Polymarket `POST /books`) — plus The Odds API key as a query param for market data. (Wording amended 2026-07-10; substance unchanged — base.py enforced this reading since H7. Scope clarified 2026-07-14: this rule governs MARKET-DATA endpoints; the Anthropic API used by rule 11's matcher is not market data, is governed by rule 11's budget and cache, and must never fetch or influence prices.)
2. **Never spend Odds API credits in tests or loops.** The free tier is 500 credits/month. All tests run against recorded fixtures in `tests/fixtures/`. Live calls happen only in `scan`/`settle` commands. Log the `x-requests-remaining` response header on every live call; warn below 100, abort the scan below 50.
3. **Do not invent API endpoints or fields.** Use only the endpoints listed in §6 (verified working June–July 2026) or verified live afterward and dated in `data/verified_facts.md` — the living endpoint registry (wording amended 2026-07-14 to match standing practice). If one returns 404/4xx, stop and report — do not guess alternatives.
4. **No magic numbers.** Every threshold, fee rate, bankroll figure, and schedule time lives in `config.yaml`.
5. **Money math uses `Decimal`, never float**, in pricing, fees, stakes, and the ledger. Floats are permitted only for display and for statistical estimation internals (de-vig power/Shin solvers, bootstrap resampling) whose results are immediately re-quantized to 6-decimal `Decimal` (wording extended 2026-07-14 for the regrade bootstrap; substance unchanged).
6. **Cross-venue opportunities are REVIEW-only by default.** Displayed but never auto-"filled" into the clean lane unless the venue-pair + market type is whitelisted in `config.yaml` after the §18.2 gate. Settlement rules differing between venues is the main source of fake arbs.
7. **Persist every quote** observed during a scan to SQLite, even when no opportunity is found. This is the backtest data.
8. **The math test vectors (§7) are non-negotiable.** Implement them exactly and make them pass before writing any venue adapter that uses the math.
9. **`flat_bets` is append-only.** Never UPDATE, DELETE, or retro-edit rows — *including label/flag columns* (the V1 backfill script that mutated only `quality_flag` was still a critical violation, because it could un-quarantine rows). Corrections are annotation rows in an immutable `annotations` table or new bet rows. **Mechanism (v3.3):** `flat_bets` holds entry-time facts only; every fact that arrives after entry — confirmation price, lifetime close, settlement result, reconciliation — is an append-only `bet_events` row (§9, §10.3), and `metrics.py` derives `pnl_confirmed`/`result`/`settled_at` at read time. Nothing ever needs to write to an existing row, so the rule has no tension with settlement. Daily reports are always recomputed from the raw log. A CI grep-test asserts no `UPDATE flat_bets` / `DELETE FROM flat_bets` — nor `UPDATE bet_events` / `DELETE FROM bet_events` — string exists under `arb/`.
10. **SX Bet quotes the maker's side.** Taker cost = `1 - percentageOdds/1e20`. Unit-test this from a recorded fixture before the SX adapter touches the pipeline; inverting it silently flips every SX price.
11. **Non-sports matching is manual-first, Haiku-second.** AI-matched events are always `status=REVIEW`, tagged `ai_matched`, displayed and paper-tracked in a segregated lane, never in the headline, and never eligible for the trusted path without a human `nonsports_map.yaml` entry. Verdicts cache append-only with a per-day budget; a settlement mismatch writes a negative verdict and permanently bans the pair.
12. **One eligibility predicate.** A single module (`arb/eligibility.py`) decides `(eligible, reason, lane)` for every bet-shaped object; detection, tracker, settle, reconcile, capacity, exposure, and report all import it. A grep-test proves the logic exists in exactly one place. (New in v3 — the V1 dual-track foot-gun produced 314/314 forbidden settled arbs.)
13. **Nothing restarts on a dirty tree.** Loops run only under the supervisor, from a committed HEAD, after a green `pytest`. A fix that is "in the tree" but uncommitted does not exist.

# Project layout

Clean sectioning, one concern per directory, modules under ~300 lines (split when larger). The `experimental/` quarantine of V1 grew to 30+ modules; v3 promotes the proven ones into first-class packages and drops the debunked ones.

```text
ArbitrageModel/
├── CLAUDE.md                  # hard rules + workflow (copy §4 verbatim)
├── SPEC.md                    # this document, as the in-repo source of truth
├── PREREGISTRATION.md         # frozen hypotheses, gates, amendment log (§18)
├── README.md                  # setup, free API key, reading the daily report (write LAST)
├── requirements.txt           # pinned (exact versions): requests, rich, rapidfuzz,
│                              #   python-dotenv, pyyaml, pytest; anthropic = optional extra (AI matcher)
├── config.yaml                # every threshold in the system (§15)
├── .env.example               # ODDS_API_KEY=..., ANTHROPIC_API_KEY=...
├── .gitignore                 # .env, data/*.sqlite*, logs/, reports/, .locks/
├── arb/
│   ├── __init__.py  __main__.py  cli.py  config.py
│   ├── models.py              # Event / Quote / Opportunity dataclasses (§7.1)
│   ├── store.py  schema.py  migrations.py     # SQLite WAL layer (§9)
│   ├── eligibility.py         # THE predicate: (eligible, reason, lane) (§12)
│   ├── venues/
│   │   ├── base.py            # adapter interface: fetch_markets() -> list[Quote]
│   │   ├── kalshi.py  polymarket.py  sx.py    # the three venues (§6.1–6.3)
│   │   ├── oddsapi.py         # consensus + scores (§6.4)
│   │   ├── mlbstats.py        # free settlement oracle + start-time truth (§11)
│   │   └── fees.py            # single leg_fee_cost() + FEE_BASIS map (§6.5)
│   ├── matching.py            # event keys, aliases, doubleheader clustering (§8)
│   ├── pricing.py             # de-vig, EV, arb math — the §7 vectors live here
│   ├── arbitrage.py           # detection: true arb, divergence, guards (§8.4)
│   ├── negrisk.py             # intra-Polymarket basket arb
│   ├── tracker.py             # flat-stake append-only ledger (§10) — the heart
│   ├── settlement.py          # settle + tripwire + timing guards (§11)
│   ├── reconcile.py           # per-venue resolution cross-check (§11.2)
│   ├── settlement_matrix.py   # per-sport scenario matrix, trusted gate (§11.4)
│   ├── markout.py             # +30s/+2m/+10m/+30m marks per booked leg (§10.5)
│   ├── invariants.py          # check-invariants command (§10.4)
│   ├── heartbeat.py           # supervisor, heartbeats, stale-data alarm (§13.2)
│   ├── report.py              # terminal tables + daily CSV/MD writer (§14.2)
│   ├── metrics.py             # ONE place computing every displayed number (§14.1)
│   ├── activity.py            # sport-dormancy gate (zero credits when out of season)
│   ├── exposure.py  capacity.py  sizing.py    # projections, never headline (§13.4)
│   ├── nonsports/
│   │   ├── matcher.py         # manual map + fuzzy candidates
│   │   ├── ai_matcher.py      # Haiku verdicts, cache, budget (§8.5)
│   │   └── strike_match.py    # deterministic strike alignment for series
│   └── lanes/                 # optional loops, each supervisor-owned
│       ├── verify.py          # flash re-quote of fresh arbs (10s); doubles as the
│       │                      #   leg-failure fill oracle (addendum SS4, v3.5)
│       ├── hot.py             # 1s-resolution lifetime watcher per fresh arb
│       ├── btc.py             # same-oracle BTC window arb (with settle path)
│       └── political.py       # intra-market YES+NO<1 (Polymarket only, §12)
├── data/                      # arb.sqlite, aliases.json, kalshi_series.json,
│                              # nonsports_map.yaml, settlement_matrix.yaml
├── reports/daily/             # auto-written YYYY-MM-DD.csv + .md
├── logs/                      # scan.log, unmatched.log, nonsports_candidates.log,
│                              # supervisor.log
├── prompts/                   # numbered work-order queue (the V2 pattern; keep it)
└── tests/
    ├── fixtures/              # one recorded real response per venue per market type
    ├── test_pricing.py  test_arbitrage.py  test_matching.py
    ├── test_tracker.py  test_settlement.py  test_eligibility.py
    ├── test_fees.py  test_negrisk.py  test_invariants.py
    └── test_hygiene.py        # the grep-tests (no UPDATE flat_bets; one predicate)
```

Deliberately **absent** from v3 (debunked or never-launched in V1/V2 — do not rebuild): `paper.py`/`positions`/`ledger` bankroll auto-fill executor (superseded by `flat_bets`), the whale tracker, the manual `forecasts` ledger, and divergence ledger rows. The record of *why* lives in §3.

# Data sources

All verified live June–July 2026. All keyless except The Odds API. Observed per-venue rate limits are **dated facts** like fees (record the limit and `confirmed_on` beside each adapter; the weekly shape-check alarms on rising 429 rates) — the keyless data supply is a courtesy that can be withdrawn, and its loss must be loud (v3.4). Every adapter is fixtures-first: record one real response per venue per market type into `tests/fixtures/` before writing the parser, and test against the fixture forever after.

## Kalshi

- Base `https://api.elections.kalshi.com/trade-api/v2`, no auth for GETs. Docs: `https://docs.kalshi.com`.
- `GET /markets?series_ticker={S}&status=open&limit=200` (cursor pagination). Discover sports series once via `GET /series?category=Sports` and cache to `data/kalshi_series.json` (winner *and* totals series; NBA totals observed as `KXNBATOTAL-...`). Non-sports series come only from `data/nonsports_map.yaml`.
- Verified fields: `ticker, title, yes_sub_title, close_time, status, yes_ask_dollars, yes_bid_dollars, no_ask_dollars, no_bid_dollars, liquidity_dollars, market_type:"binary"`. Decimal-dollar strings parse straight to `Decimal`. Kalshi's YES and NO sides are **one order book**: `no_ask = 1 − yes_bid` by construction, so intra-market YES_ask + NO_ask < 1 is algebraically a crossed book (`yes_ask < yes_bid`) — a feed artifact, never an arb; the political lane therefore never scans Kalshi (§12).
- **Empty-book trap:** ask 1.0000 + bid 0.0000 + liquidity 0 is an empty book, not a quote. Drop it.
- Orderbook: `GET /markets/{ticker}/orderbook` — use for the `min_executable_usd` depth check and for multi-level ladder capture (§13.4). Kalshi's book size units are under-documented: document the conversion you verify against a live book, and unit-test depth from a fixture. Markets group under an `event_ticker`; mutually exclusive event groups feed the Kalshi negRisk scan (§2). A candlestick-history endpoint may exist for markout backfill — verify per rule 3 before relying on it.
- **Start time:** parse from the ticker's embedded time. **Never fall back to `close_time`** — it is days after the game and corrupted event keys, in-play guards, and settlement bucketing in V1 (bug D1). A quote whose start can't be parsed is `start-unknown` and non-actionable.
- Kalshi quotes totals as an exhaustive half-point strike ladder (2.5→12.5, 11–25 lines per game). Persist every strike; detection pairs each strike against other venues' main lines (§8.3).

## Polymarket

- Gamma discovery: `GET https://gamma-api.polymarket.com/events?tag_slug={mlb|nba|soccer|tennis|ufc}&closed=false&limit=100`. Markets carry `question, outcomes, outcomePrices, clobTokenIds, endDate, liquidity, gameStartTime`.
- **MMA keys on `tag_slug=ufc`, not `mma`** (the `mma` tag went futures-only mid-2026). Canonical sport string stays `mma` everywhere else. UFC titles carry a `"(Weight, Card)"` suffix — strip before name parsing. In-scope: `moneyline` → WINNER, `totals` (round totals) → TOTAL; ignore method-of-victory / go-the-distance markets.
- **Executable prices come from the CLOB only:** `GET https://clob.polymarket.com/book?token_id={id}`. Gamma `outcomePrices` are stale mids — never use them for detection. Fetch the full per-token book (not just top-of-book) so arb legs carry real multi-level ladders (V1 bug B4: every Polymarket leg was permanently single-level, capping the whole capacity model). Per-token GETs cannot cover hundreds of active tokens inside `venue_fetch_timeout_secs` (15) — the exact recipe for a re-run of V1's 140s cadence lie. Verify the CLOB **batch books endpoint** (`POST /books`, list of token_ids) per rule 3 and use it; if none exists, pin a per-cycle token budget prioritized by active fingerprints and open bets, and print the achieved book-coverage fraction in the daily report — silent partial coverage is the failure, not partial coverage itself.
- **Depth trap:** never let event-level `liquidity` stand in for one outcome's depth. Unknown depth is `None`, and `None` means non-actionable, not infinite.
- **Tennis start times from Gamma are nominal** (follow-on court scheduling, hours late). Mark polymarket+tennis starts unreliable; an event whose only start source is unreliable is start-unknown → non-actionable.
- Free keyless WebSocket book feed exists; it is the real fix for both sparse markout coverage (§10.5) and full-ladder capture, so it leads the Phase-8 queue.

## SX Bet

- Base `https://api.sx.bet`, no key. Docs: `https://api.docs.sx.bet`.
- `GET /sports` (Basketball=1, Baseball=3, Soccer=5, Tennis=6, MMA=7), `GET /leagues/active?sportId={id}`, `GET /markets/active?leagueId={id}&onlyMainLine=true` (paginate `nextKey`). Market `type`: **226 = moneyline, 28 = totals** (in scope); 342 = spread (ignore). Fields: `marketHash, teamOneName/teamTwoName, outcomeOneName/outcomeTwoName, line, gameTime, status, outcomeVoidName`.
- Order book: `GET /orders?marketHashes={h}`. Per order: `percentageOdds` = the **maker's** implied probability x 1e20 (string), `isMakerBettingOutcomeOne`, sizes in USDC x 1e6.
- **Taker math (rule 10):** you take the opposite side. Cost per $1 payout of outcome one = `1 - percentageOdds/1e20` over orders where `isMakerBettingOutcomeOne == false`; best price = max `percentageOdds`. Depth at a level is the *taker* dollar notional: `maker_stake * c / (1 - c)`. Unit-test both from a fixture before the adapter ships.
- SX void outcomes (`NO_CONTEST`, `NO_GAME_OR_EVEN`) differ from Kalshi/Polymarket binary rules → SX legs start REVIEW until whitelisted per sport. SX legs cannot be flash re-quoted from stored rows (outcome side is not re-derivable) — mark them `unverifiable` in the verify lane rather than silently skipping. This section lists no settled-market endpoint: **verify one exists before Phase 6** — SX reconciliation depends on it, and if none exists, SX legs can never leave REVIEW (state that outcome explicitly rather than rediscovering bug A2).
- SX quotes are slow/unrepriced; the polymarket+sx pair's low vanish rate is an artifact of that, not durable edge. Price SX honestly (win fee, thin depth) before believing anything on that pair.

## The Odds API (consensus + scores; optional, credit-metered)

- `GET https://api.the-odds-api.com/v4/sports/{key}/odds/?apiKey={K}&regions=us&markets=h2h,totals&oddsFormat=decimal`. Cost: 1 credit per market per region — so h2h+totals = 2 credits/sport/scan. `GET /v4/sports/` is free (discover soccer/tennis/MMA keys there).
- Scores for settlement: `GET /v4/sports/{key}/scores/?daysFrom=3` (3 = the API max lookback; historically 2 credits with `daysFrom` — verify against the `x-requests-last` header on the first live call and correct the budget math). MLB Stats API (`statsapi.mlb.com`, free, keyless, unlimited history) is the primary MLB settlement oracle and backfill rescue; the Odds API covers the rest.
- Tennis/MMA venues quote WINNER only → fetch h2h only for those keys (don't waste a totals credit that can never join a venue quote).
- Sport-key aliasing: the Odds API exposes `soccer_fifa_world_cup` while venues key events as `soccer`; map keys to canonical prefixes in config so consensus rows join venue quotes. Pinnacle is exposed under `regions=eu`, not `us` — the `consensus_anchor` experiment fetches it explicitly, **h2h only** (v3.4): the full h2h+totals anchor costs 4 scans × (2 us + 2 eu) + 2 scores = 18 credits/day = 540/month — over the 500 free-tier budget, so the abort guard would truncate the pre-registered experiment every month-end with a time-of-month-biased sample. H2h-only is 4 × (2+1) + 2 = 14/day = 420/month, inside budget. (Alternative considered: 2 scans/day with the full anchor — rejected because it widens the §13.1 consensus-freshness dead zones.)
- Log `x-requests-remaining` on every call; warn < 100, abort < 50. Never log full request URLs — the key rides as a query param; every log sink redacts query strings (one regex, one test). `consensus_enabled: false` disables everything.

## Fee models (single implementation, dated facts)

All fees route through one function, `leg_fee_cost(venue, category, price, contracts)`, driven by a `FEE_BASIS` map. Fees are **dated facts**: each venue's config block carries `source_url` and `confirmed_on`; entries older than `fee_stale_days` (30) get a load-time warning and a stale-fee badge in reports (never a silent halt). V1 lost ~0.75pts of pricing accuracy — more than the median arb edge — to silent fee drift.

| Venue | Model (per $1-payout contract) | Rates (confirmed 2026-07-02) |
|---|---|---|
| Kalshi | taker `ceil_to_cent(0.07 * C * P * (1-P))` (money vector: 10 contracts @ P=0.40 → ceil(0.168) = $0.17) | taker 0.07, maker 0.0175 (25% of taker — reduced, not rebated); index markets 0.035 (out of scope); schedule effective 2026-06-29 |
| Polymarket | **quadratic per category**: `fee = C * feeRate * p * (1-p)`; makers 0 | sports 0.03, crypto 0.07, finance/politics/tech 0.04, econ/culture/weather/other 0.05, geopolitics 0; effective 2026-03-30 |
| SX | singles 0% taker+maker; **5% on winning parlays only** (model in the parlay lane) | help.sx.bet fee page |

Historical stored quotes always keep their as-observed `fee_cost`; a fee-model change is a versioned amendment affecting newly fetched quotes only. NegRisk uses the same `leg_fee_cost` (V1's inline second fee implementation used the wrong curve and manufactured fake basket arbs — bug C0). Add an execution-side `taker_price_buffer` (default $0.005/contract) on top of fees so displayed edges survive latency/queue risk — applied as an addition to each flagging **threshold** (an edge must clear its floor plus the buffer); stored costs and booked P&L remain as-observed, since inflating stored costs would corrupt the measurement; excluded from the §10.5 markout drift term, which measures mid movement only. (Pinned v3.4 — previously ambiguous which surfaces included it; alternative considered: fold the buffer into effective cost, rejected because it synthetically distorts the ledger.) `leg_fee_cost` takes a `mode` argument: `paper` (continuous curve, fractional contracts — the flat-$1 ledger) vs `execution` (whole contracts, Kalshi `ceil_to_cent`); the §7 vectors state which mode they exercise.

# Canonical model and math

## Data model (`models.py`)

Everything in **cost per $1 payout** (= implied probability), `Decimal` quantized to 6 places, ROUND_HALF_EVEN. Quantization boundary (v3.4): computations run at full precision; quantize exactly at store/compare boundaries, never mid-chain (the §7.2 vectors agree either way only by luck of the digits).

- `Event(event_key, category, sport, home, away, start_utc, start_grade)` — `event_key = sport:date:sorted(norm(home), norm(away))` where `date` is the start's calendar date **in America/New_York**, computed from the normalized UTC instant — never a venue-local or UTC date (a 9pm EST start is next-day UTC; a key mismatch defeats the ±6h window entirely, since differing key strings never meet). Non-sports events key from their `nonsports_map.yaml` id. `start_grade ∈ {verified, nominal, absent}` per the truth hierarchy (§11.3).
- `Quote(event_key, venue, book, market_type{WINNER|TOTAL}, line|None, outcome_label, side{YES|NO}, raw_price, cost, fee_cost, depth_usd, ladder_json|None, live, fetched_at)` — `outcome_label` ∈ normalized team / `DRAW` / `OVER` / `UNDER` and names the **underlying proposition**; `side` (v3.4) is which side of it this quote buys. Complement-mapping a NO quote to the opposite outcome (`NO(A) → YES(B)`) is permitted **only** for strict two-outcome binaries with no third state: MLB/NBA winner, yes; soccer winner, **never** — `NO(A wins)` = {B ∪ draw}, a composite no team label can carry, and mapping it to `YES(B)` silently rebuilds the draw-hole fake arb beneath every §8.4 guard; integer-line totals, **never** — `NO(Over 9)` = under-or-push ≠ `UNDER 9`. Where the map is illegal the quote keeps `(outcome_label, side)` unmapped, and the endorsed cross-venue soccer construct pairs on (same proposition, opposite sides). Where legal, the map runs **before** fingerprints/position keys are built, so one opinion keys identically however quoted. Fixture: a soccer `P-NO(TeamA)` quote must never acquire `outcome_label = TeamB`. Effective cost = `cost + fee_cost`.
- `Opportunity(kind, category, event, legs, edge, stakes, status{OK|REVIEW}, notes, fingerprint, position_key)`:
  - `fingerprint = sha1(event_key | kind | sorted((venue, book, market_type, line, outcome, side) per leg))` — prices excluded, so a drifting price is the same opportunity; `side` included post-complement-map (v3.4).
  - `position_key` — the *economic* position, independent of venue, defined **per kind**: single-leg kinds (EV, DIVERGENCE) `(sport, event_key, normalized_outcome, market_type, line)` — post-complement-map, so `NO(A)` and `YES(B)` on a strict binary are one position; where the map is illegal (soccer, integer lines) the tuple gains `side` (v3.4); ARB/XSPREAD `(sport, event_key, market_type, line, 'COVER')` (outcome-less — a covering position holds every outcome); NEGRISK `(category, event_key, 'BASKET', basket_sha)` with `basket_sha` = sha1 of the sorted outcome token ids. One opinion re-quoted on another venue pair is the same position, not a new bet (V1 bug C2: 661 rows collapsed to 148 fingerprints and even fewer positions; every aggregate was inflated 4.5x).
- `edge` / `edge_points` are always **probability points per $1 of payout** (a fraction: 0.02 = 2 points), never a return ratio (v3.4 — previously undefined, and the system mixes two edge units): EV kinds `p_hat − c`; locked kinds `1 − S/P` where `P` is the guaranteed payout per unit basket (1 for 2-leg arbs and YES baskets, `N−1` for NO baskets). ROI (`1/S − 1`, `(p_hat − c)/c`) is display-only, derived in `metrics.py`. The §8.4 skew decay and the §10.5 markout rule operate on points — feeding them a ROI mis-scales the haircut by `1/c` (2x at c = 0.5).

## Mandatory test vectors (exact, `Decimal`; implement in Phase 2, before any adapter)

**De-vig (proportional, per book):** `p_i = (1/o_i) / sum_j(1/o_j)`.
Odds (1.91, 1.91) → (0.5, 0.5). Odds (1.50, 2.86, 6.00) → normalizer k = 1.183 → approximately (0.5635, 0.2956, 0.1409), summing to 1.

**De-vig methods:** implement `proportional`, `power`, and `shin`; config-selected. The archive bake-off (n=30 settled MLB) ordered power (0.22041) < shin (0.22099) < proportional (0.22217), but n=30 cannot separate those Briers — treat `power` as a coin-flip default, not an empirical winner. Pre-registered re-run at n ≥ 200 settled quotes; changing the default is a dated amendment. Store per-book de-vigged probabilities so flags can be re-graded under another method without re-running — they persist in `consensus_books` (§9); the median-only `consensus` table cannot support the pre-registered n ≥ 200 bake-off re-run.

**Consensus:** median de-vigged p across ≥ `min_books_for_consensus` (3) books; totals threshold 2 (line fragmentation would otherwise kill totals consensus). Optional outlier drop: exclude a book whose de-vigged prob is > `consensus_outlier_points` from the cross-book median — disabled below 3 books (dropping one of two leaves a one-book "median"); n=2 consensus rows (the totals floor) are tagged `thin-consensus` in reports and edge buckets.

**EV:** `c = cost + fee_cost`; `EV = (p_hat - c) / c`.
Vector: p_hat = 0.55, Kalshi ask 0.50, fee = 0.07 x 0.5 x 0.5 = 0.0175 → c = 0.5175 → **EV = 0.062802**. (Paper-ledger fees use the continuous curve — no cent-ceiling — because flat-$1 stakes buy fractional contracts; `ceil_to_cent` applies only to whole-contract execution vectors like the $0.17 money vector below.)

**Kalshi fee (money):** 10 contracts @ P = 0.40 → ceil_to_cent(0.07 x 10 x 0.40 x 0.60 = 0.168) = **$0.17**.

**Polymarket quadratic fee:** at p = 0.50, sports rate 0.03 → fee per contract = 0.03 x 0.5 x 0.5 = **$0.0075** (the linear model's $0.015 here was the double-charge bug).

**True arb:** best effective cost per outcome `c_i`; `S = sum(c_i) < 1` → `ROI = 1/S - 1`; `stake_i = B * c_i / S`. (`stake_i ∝ c_i` ⇔ **equal contract counts** B/S on every leg — the identity that makes this same formula lock 2-leg arbs, YES baskets (payout B/S), and NO baskets (payout (N−1)·B/S); do not invent a different NO-side allocation.)
Vector: decimal odds 2.10 & 2.05 → S = 0.963995 → **ROI = 0.037350** (unrounded 0.0373498…; all quantization in this spec is ROUND_HALF_EVEN to 6 places); B = $100 → stakes $49.40 / $50.60, payout $103.73–103.74.
No-arb vector: 1.95 / 1.95 → S = 1.025641 (must NOT flag). Covering-set vector: soccer K-YES(TeamA) 0.45 + P-YES(TeamB) 0.45 sums to 0.90 and must NOT flag — a draw loses both legs.

**NegRisk fee vector:** a basket that is exactly break-even under the correct quadratic fee must NOT flag as an arb (regression against bug C0). NO-side vector: 3 outcomes, NO asks (0.65, 0.66, 0.67) → sum 1.98 < 2 → flags (pre-fee profit $0.02 per basket, ROI = 0.02/1.98 = 0.010101); the same basket at (0.67, 0.67, 0.67) → sum 2.01 must NOT flag. Exhaustiveness vector (v3.3): a 3-outcome group whose YES asks sum to 0.95 but whose YES **mids** sum to 0.93 must flag REVIEW `unproven-exhaustive`, never clean — regardless of venue flags (the mids-sum check is an independent second condition).

**Divergence:** `spread = mid_A - mid_B` per outcome, on **raw mids** (no fees — it is a signal, not a P&L claim); DIVERGENCE if |spread| ≥ threshold and no arb survives asks + fees. Persist all spreads (quotes only — no ledger rows).

**Parlay (Phase 7):** legs p = (0.60, 0.55) → Fréchet bounds [max(0, 0.60+0.55-1), min(0.60, 0.55)] = [0.15, 0.55]; independence fair = 0.33. Ask 0.12 → class-1 flag (below lower bound); ask 0.25 → class-2 flag (vs. fair, threshold 0.05).

**Tracker vector:** see §10.7.

Totals use identical math with OVER/UNDER as the two outcomes.


# Matching

## Names and aliases

Normalize (lowercase, strip punctuation and city qualifiers) into `data/aliases.json`, seeded with all MLB/NBA teams plus short forms. **Alias tables are per sport** — "Athletics"/"Rangers" collide globally. Soccer needs care within the sport too: one `soccer` key spans every league, and city-qualifier stripping maps "Manchester United" and "Atlanta United" toward the same token — single-team fuzzy similarity is never sufficient for soccer (both teams must match; alias entries may be league-scoped). Tennis/MMA use surname + first initial + event date (bare surnames collide on siblings and doubles), and EV bets on surname-matched sports require an explicit per-player alias entry (fuzzy matches may record divergences but never EV bets — this killed V1's fake tennis edges).

## Event identity

Same sport + same unordered team pair + start within ±6h → same `event_key`. Candidate grouping also bridges **date±1**: a team pair matching within 6h across adjacent NY dates (a 23:50 vs 00:10 listing) is one candidate group — differing key strings otherwise never meet. When a bridge matches, the group re-keys to one **canonical `event_key`** before anything persists: the NY date of the `verified` start if one exists (§11.3), else the earliest venue-listed start; the losing key goes into an alias map so later quotes from the drifting venue re-key deterministically. Without canonicalization one game lives under two key strings and every event_key join — consensus, markouts, position_key dedup, CI clustering — silently splits (bug C2's inflation through a new door). Exact alias match → auto. `rapidfuzz.token_set_ratio ≥ 92` → match, but forced `REVIEW`/`fuzzy-match`. Unmatched → `logs/unmatched.log`, never forced. Two sanity gates run before any pairing: venues must agree on start time within `max_start_drift_mins` (30), and the same outcome must not be priced more than `match_suspect_prob_diff` (0.15) apart — a 20-point gap is a matching error, not an arb; suspect groups can log divergences (tagged `match_suspect`, excluded from divergence/lead-lag analyses) but never ARB flags.

**Doubleheader identity (fix for V1 bug D2):** the date-only event key means two MLB games between the same teams on one day collide. Primary disambiguation for MLB is the MLB Stats API schedule's `gamePk`/`gameNumber` (already fetched for start-time truth, §11.3) — deterministic, no heuristic; the clustering below is the backstop for venues/sports without a schedule oracle. Within a matched market group, cluster legs by start time (gap > `doubleheader_cluster_tol_secs`, default 7200s, = different game) before pairing outcomes, so game-1 and game-2 quotes never combine into a fake arb. The start-drift gate stays as a backstop.

## Totals: exact line, every strike

Totals match only on the **exact same line** (8.5 ↔ 8.5; never interpolate; 8.5 ≠ 9.0 — a push changes the contract). **Ladder-aware requirement (Stage-α finding):** Kalshi quotes an exhaustive strike ladder that contains Polymarket/SX main lines in ~100% of multi-venue MLB totals events (21/21 live, 270/275 archive). Detection must therefore key outcomes by `(market_type, line, outcome)` and pair **every** Kalshi strike against other venues' lines, not a single main line. Totals are ~50% of live quote inventory; a main-line-only matcher silently discards half the surface. Integer lines can PUSH — flag them, and optionally scale edge by (1 − push probability) from a Poisson run-total model (config `push_pricing`). `line` is `Decimal`/string wherever it appears in a key or comparison — never float (float 8.5 equality is representation-dependent); test: a line parsed from a JSON number and from a string produce identical keys.

## Detection guards (all at detection time, not just settle time)

- **In-play/final guard:** drop any leg whose `start_utc <= now` (route to a live lane if one exists); a resolver saying the event is final records a `FINAL_EVENT_SKIPPED` diagnostic, never an arb. (V1 booked arbs on finished games.)
- **Start-unknown guard:** a leg with `start_grade = absent` is never actionable.
- **Simultaneity:** legs fetched more than `max_actionable_skew_secs` (10) apart were never live together → REVIEW with a skew note. Additionally decay the edge continuously: `adj_edge = edge_points - skew_secs * line_velocity_points_per_sec[sport]`; REVIEW if `adj_edge <= min_adj_edge_points`. The old binary 60s window is only the outer recording bound.
- **Edge-sanity cap:** a computed edge above `ev.max_edge` (0.15) or `edge_suspect_above` (0.25) is a data error (stale/one-sided book, mis-joined consensus) — record the quote, book no bet, count it in "left on the table" with reason `edge-implausible`. The largest V1 raw "edge" was ~54,500%; the largest clean one was 18.2%. Locked-kind ROI above `arb_stale_warn` (0.04) books REVIEW automatically — for **every** locked kind (ARB, XSPREAD, NEGRISK, POLITICAL, BTC), not only cross-venue XSPREAD: the headline-eligible single-venue kinds are exactly where a stale-book 8% "arb" would otherwise sail into clean P&L unguarded. The project's own priors say a 10% "arb" is near-certainly fake, and a log line is not a guard.
- **Depth floor:** every leg needs `depth_usd >= min_executable_usd` (50) to be clean; unknown depth routes to the micro lane (EV-only, larger edge floor, stake capped by visible depth) or is non-actionable.
- **Covering-set proof:** ARB legs must provably cover the event's full outcome space under **both** venues' resolution rules. Soccer WINNER markets are binary lose-on-draw: K-YES(TeamA) + P-YES(TeamB) at combined cost < 1 is NOT an arb — a draw loses both legs; the only valid cross-venue soccer construct is same-outcome YES/NO. A leg with a void outcome (SX draw-no-bet via `outcomeVoidName`) can never appear in a covering set with a lose-on-draw leg. Fixture: K-YES(A) 0.45 + P-YES(B) 0.45 must NOT flag. **The same proof binds NEGRISK (v3.3)** — `require_exhaustive_exclusive` was previously an intention with no mechanism. A basket is proof-passing only if (a) the venue structurally asserts the group: Polymarket's negRisk flag (protocol-enforced one-winner) or Kalshi's event-level `mutually_exclusive: true` field (verify the field exists per rule 3 before Phase 5; if it doesn't, Kalshi groups are REVIEW-only, stated as such) — **and** (b) the mids-sum sanity check passes: sum of YES mids across the group within `[negrisk.exhaustive_proof.mids_sum_min, mids_sum_max]` (0.995–1.10 after dry-run patch A1). Mutual exclusivity does not imply exhaustiveness: YES mids summing to 0.93 means ~7% of the outcome space is unlisted and the "arb" is a hidden short in that tail — **and** (c) every listed outcome carries a two-sided, non-zombie book (v3.4): an unquoted outcome cannot enter the mids sum, so excluding it silently weakens the proof — a group missing a 3% outcome plus one unquoted tail would otherwise slip under the window. Thin tail books also inflate mids upward (a 0.001/0.05 book mids at 0.0255 vs true ~0.01), so a *min*-side pass on a wide basket is more suspicious, not less. Failing baskets flag REVIEW with reason `unproven-exhaustive`, never clean — **and** (d, dry-run patch A2) the proof's inputs must be sharp enough to mean anything: if the summed half-spreads across the group exceed `negrisk.exhaustive_proof.max_half_spread_sum` (pinned equal to the window slack `1 − mids_sum_min`; the loader asserts the equality so the two can never drift), the mids sum can mask a tail larger than the tolerance → `unproven-exhaustive`. **Coupling invariant (dry-run patch A1):** the tail the window tolerates must be smaller than the smallest edge the clean lane admits — `negrisk.min_edge > (1 − mids_sum_min)/mids_sum_min`, enforced at config load. The prior defaults violated it: at cost 0.985 with a hidden 1.9% tail (inside the old 2% tolerance), EV = 0.981 × 0.0152 − 0.019 = **−0.41 points** — a worst-case-negative basket booked into the headline as "locked." Non-negativity in every resolution state requires margin m ≥ p_tail/(1 − p_tail); the config pair (0.006, 0.995) satisfies it with slack.
- **Zombie guard:** a quote with zero 24h volume and (ask − bid) ≥ `zombie_spread_min` (0.90) is a zombie book, not a market — tagged, excluded from detection and markouts (generalizes the empty-book trap). Volume source is pinned, not assumed: Kalshi `volume_24h` on the market object; Polymarket Gamma `volume24hr` (Gamma staleness is acceptable for a volume guard, unlike for prices) — verify both fields per rule 3 and add them to the §6 fixtures before the guard ships.
- **Rejected-arb audit trail:** every rejection writes a diagnostic row with its reason (V1 silently `continue`d, losing the record of what was rejected and why).

## Non-sports matching

Manual-first: the scanner writes fuzzy candidates (title similarity ≥ 85) to `logs/nonsports_candidates.log`; a human promotes pairs into `data/nonsports_map.yaml`. Human entries always win and are the only path to any trusted lane. Second lane, when `ai_matcher.enabled`: Haiku (`claude-haiku-4-5`) judges pair equivalence from both venues' resolution rules (truncated to `max_rules_chars`, keeping head + tail halves — resolution edge cases live at the end of rulebooks), min confidence 0.85, budget `max_new_pairs_per_day`, verdicts cached append-only at market level with a rules-hash (`rules_sha`) so an edited rulebook expires its cached verdict. Multi-market series (weather/macro strikes) pass through a deterministic strike matcher first — exact comparator/threshold/unit alignment — so only strike-aligned pairs reach Haiku. SX pairs are never AI-matched (no rules text). AI-matched flags: always REVIEW, tagged `ai_matched`, paper-tracked flat-$1 in a segregated lane; an inconsistent cross-venue settlement flags `settlement_mismatch`, writes a negative verdict, and permanently bans the pair. **Prerequisite before any budget expansion:** a hand-labeled ground-truth set plus a stage-1 recall harness — V2 discovered recall had never been measurable because no confirmed-equivalent pair ever existed. Bootstrap the harness with sports pairs first: hundreds of confirmed-equivalent (and known-different) sports matches already exist in the manual map — running the judge over those bounds recall/precision for cents before any non-sports budget is spent.

# Storage (SQLite, WAL)

Stdlib `sqlite3`, no ORM. WAL mode. All money columns TEXT holding `Decimal` strings (never REAL; only pure time measurements like `leg_skew_secs` may be REAL). Schema versioned via a tiny `migrations.py` that only ever adds.

```sql
quotes(id, poll_id, fetched_at, event_key, category, sport, venue, book,
       market_type, line, outcome, side, raw_price, cost, fee_cost, depth_usd,
       ladder_json, live)                -- every quote, every poll, forever
       -- side per SS7.1 (v3.4); book = sportsbook for oddsapi rows, = venue
       -- for venue-native rows
polls(id, started_at, kind, n_quotes, n_opps)   -- kind: watch|scan|break|verify|...
poll_venues(poll_id, venue, ok, n_quotes, latency_ms)  -- per-venue fetch status
opportunities(id, poll_id, fingerprint, position_key, kind, category, event_key,
       edge, status, legs_json, notes)
flat_bets(id, created_at, poll_id, poll_kind, fingerprint, position_key, kind,
       category, sport, venue_pair, event_key, event_start_utc, start_grade,
       legs_json, stake_usd, edge_points, trusted, lane, quality_flag,
       consensus_age_secs, leg_skew_secs, status, pnl_optimistic,
       reappearance_of, notes)
       -- ENTRY-TIME FACTS ONLY, APPEND-ONLY (rule 9): status is the detection
       -- status (OK|REVIEW), pnl_optimistic is knowable at entry; anything
       -- that happens later is a bet_events row, never a column write
       -- quality_flag ∈ {none, empty_book, one_sided, match_suspect,
       -- stale_consensus, edge_implausible, zombie} (pinned v3.4); micro and
       -- live are category-level tags, NOT lanes — lane stays {clean,review,invalid}
bet_events(id, bet_id, created_at, kind, payload_json)
       -- append-only lifecycle log; kind: confirmed (second-sighting prices)
       -- | lifetime_closed | settled (result + pnl basis) | void | reconciled
       -- at most one settled row per bet (terminal); metrics.py derives
       -- pnl_confirmed / result / settled_at at read time, pnl_markout from
       -- the markouts table — no derived value is ever stored back
       -- payload_json pinned per kind (v3.4): confirmed={per-leg effective
       -- costs, poll_id}; settled={result|fraction, source, per-leg outcomes};
       -- reconciled={venue, verdict}; lifetime_closed/void={reason}
annotations(id, created_at, target_kind, target_id, kind, detail)
       -- append-only; target_kind: bet|poll|day
consensus(event_key, market_type, line, outcome, p_hat, method, n_books,
       dispersion, computed_at)          -- dispersion = IQR of per-book
                                         -- de-vigged probs (pinned v3.4)
consensus_books(event_key, market_type, line, outcome, book, raw_odds,
       p_devig, method, computed_at)   -- per-book de-vig inputs and outputs;
       -- the SS7.2 bake-off re-run and any regrade read these, never the median
markouts(id, bet_id, leg_index, horizon_secs, actual_offset_secs, mark_cost, marked_at)
heartbeats(process, beat_at, head_sha, pid)
verdicts(id, created_at, pair_key, verdict, confidence, rules_sha, model, cost_usd)
left_on_table(id, poll_id, fingerprint, kind, event_key, venue_pair, legs_json,
       reason, edge_points)                -- filtered flags, reconstructable;
       -- venue_pair direct (v3.4) so per-pair populations never require
       -- parsing legs_json
blacklist(fingerprint, event_key, reason, created_at)  -- tripwire re-book bans
```

Storage rules: every table an INSERT-only or INSERT+terminal-row pattern; `arb report` recomputes any day idempotently from raw rows, never from stored aggregates — using the **stored** `trusted`/`lane` columns: eligibility is never re-derived at report time, so config changes affect new rows only (otherwise "idempotent" recompute silently reclassifies history); pre-cutover databases are analysis-only oracles and are **never blended into post-cutover P&L**. Concurrency: `PRAGMA busy_timeout=5000`; inserts batched in short transactions; each table written by exactly one process kind (quotes/polls by the loops, flat_bets by the tracker, markouts by its hook; `bet_events` rows are namespaced by `kind`, each kind with exactly one writer — confirmed/lifetime_closed by the tracker, settled/void by settle, reconciled by reconcile) — poll-kind ownership extended to writes. Minimal indexes (more slows the 600k-row/day insert path): `quotes(event_key, fetched_at)`, `quotes(poll_id)`, `flat_bets(fingerprint)`, `flat_bets(position_key)`, `bet_events(bet_id)`, `markouts(bet_id)`.

# The flat-stake tracker — THE deliverable

Answers, per day: *"if I'd put $1 on every flag, where would I be?"* All P&L net of fees and spread crossing.

## Bet creation

First poll where a fingerprint appears → one row per class:

- `ARB`: $1 total split across legs by the §7 stake formula; the locked P&L **is** `pnl_optimistic`, known at entry (no separate `locked_pnl` column — pinned v3.4).
- `EV`: $1 on the single leg; resolves at event settlement; recorded for every EV > 0 with its edge bucket. (Creation frozen while `ev_enabled: false`; quotes and would-be flags still recorded to `left_on_table`.)
- `NEGRISK`: $1 across the basket.
- Divergences: **no ledger row** (v3 change) — persisted as opportunities/quotes only.

Classes are independent: one event can hold an ARB and an EV bet simultaneously, each in its own lane. Before creating a row, check `position_key`: an active bet on the same economic position absorbs the sighting as an improved quote (note on the existing row's annotations), not a new bet. An absorbed sighting from a *different venue pair* also writes a `left_on_table` row (reason `position-dedup`, pair attributed) so the §18.1 per-pair populations stay honest — only the bet is deduped, never the flag count.

## Lifetime, dedup, and poll ownership

A fingerprint is active while observed on consecutive polls **of the poll kind that created it**. Absent for ≥1 completed poll of that kind → lifetime closed. *Completed* is judged **per venue**: if a venue's fetch failed or timed out, fingerprints with a leg on that venue are neither confirmed nor closed that cycle (carried; poll annotated `venue-fetch-gap`) — a venue outage must never mass-close lifetimes and re-book them (that recreates the I1 duplicate storm via a plain HTTP error). Reappearance later → a new row with `reappearance_of` linking the old one. **Poll-kind ownership is mandatory:** V2's worst single bug (315 duplicate bets in a day) came from six different loop kinds sharing absent-close logic — a bet created by `watch` must be closable only by `watch` polls. An observability guard logs any fingerprint with > `max_reappearances` (10) rows in 24h.

## Dual fill columns

- `pnl_optimistic`: filled at first-observation effective prices (stored on the bet row — knowable at entry).
- `pnl_confirmed`: derived by `metrics.py` from the `confirmed` bet_events row written at the second consecutive observation — valid only while the event is still **pregame** (v3.4: an in-play second sighting never confirms; the bet counts missed with reason `in-play-at-confirm` — otherwise in-play prices contaminate the confirmed column for exactly the bets booked closest to start); no valid `confirmed` event by lifetime close → confirmed missing **and counted as "missed"** in every aggregate (the V1 survivorship artifact came from silently dropping these). Missed enters the confirmed P&L cell as **$0** — the bet was never made, neither won nor lost — displayed next to the miss rate; exclusion-with-count would re-open the survivorship door.

## Terminal discipline, tripwire, invariants

- SETTLED is terminal — but only once the event has genuinely ended (start in the past by `terminal_mute_grace_mins`). A SETTLED row for a still-future event is premature; a re-sighting re-opens via the normal reappearance path.
- **Instant-settle tripwire (resolution-settled kinds only — EV, NEGRISK, BTC, non-sports):** a bet with `settled_at - placed_at < settle_tripwire_secs` (one poll interval) had a knowable outcome at placement — never a forecast. Locked-at-entry kinds (ARB) settle at placement by design and are exempt. The row still writes (append-only) but is auto-annotated `settle-tripwire`, excluded from every P&L cell, and its fingerprint is written to the `blacklist` table (§9), barring re-booking for the event's lifetime. Any future instant-settle row without an annotation is a sev-1.
- **`arb check-invariants`**, run at the end of every settle cycle and in CI: (1) no bet settled before placement; (2) no bet placed after its event's final; (3) every resolution-settled row's event is in the past (locked-at-entry ARB rows are instead checked pregame-at-placement); (4) no fingerprint exceeds the reappearance bound un-annotated; (5) no P&L cell includes an annotated-excluded row; (6) no bet has more than one `settled` bet_events row, and none precedes its bet's `created_at` (v3.4). Silent when clean; CRITICAL log + annotation when not.
- **Hygiene grep-tests in CI:** no `UPDATE flat_bets`/`DELETE FROM flat_bets` anywhere under `arb/`; `eligibility` logic in exactly one module.

## Markouts (the real slippage/adverse-selection measure)

For every booked leg, persist the **book mid** at +30s, +2m, +10m, +30m into `markouts`, from the quotes the loops already sample. Mid, not ask — marking against the ask double-counts half-spread; mid drift is the adverse-selection measure (basis frozen here, before any data). A mark uses the nearest sample within ±50% of its horizon and records its actual offset; a horizon with no sample in tolerance stays NULL, and the daily report prints per-horizon coverage. Expect +30s coverage to be sparse and arb-biased (only hot/verify sample faster than the 60s watch cadence); never compare classes on horizons with materially different coverage. Derive `pnl_markout` as **one-sided adverse-drift-adjusted optimistic** (v3.4 — sign corrected): per booked BUY leg, drift `D_h = mid_h − mid_0` (probability points; SELL legs negate), and `pnl_markout = pnl_optimistic + Σ_legs min(0, D_h) × contracts` (dollars), using the +30s horizon and falling back to the earliest covered **pregame** horizon (recording which). Derivation of the sign: adverse selection for a buyer is the mid **falling** after the fill (`D_h < 0`). The v3.2 form (re-enter at the mark) and the v3.3 form (`pnl_optimistic − D_h`) both *increased* P&L when the mid fell — rewarding exactly the picked-off fills the measure exists to expose; they measured latency-chase cost, a near-opposite quantity. The penalty is one-sided (`min(0, D_h)`; favorable drift never credited) because realized edge already pays inside `pnl_optimistic` at settlement — crediting favorable drift would double-count momentum. (Two-sided `+D_h` was considered and rejected as less conservative.) Fees and half-spread stay in the entry basis via `pnl_optimistic`; nothing double-counts. A mark is valid only if it is **pregame** (entry is pregame by the §8.4 guard); post-start marks are stored but tagged `in_play` and excluded from `pnl_markout` and the decision rule — +10m/+30m drift on a bet placed 20 minutes before first pitch is game-state noise, not adverse selection. The daily report prints per-horizon *pregame* coverage. **Decision rule (frozen; restated mechanically in v3.4):** the rule fires for a class when `mean(D_2m) < −0.5 × mean(edge_points)` over the rolling 30-day window of *adjusted* edges, pregame-valid marks only; the class's headline then switches to `pnl_markout` and any go-live math prices `|mean(D_2m)|` as a per-bet cost. Markout ≈ 0 would be the single most valuable positive fact this project could produce. A flicker/zombie guard excludes marks from suspended or empty books.

**Tape confirmation (the fill-reality upgrade):** both venues publish trades (Kalshi `GET /markets/trades`; Polymarket CLOB trade history — verify both per rule 3 before building). When `tape_check.enabled`, a fill is `tape_confirmed` only if the venue printed a trade at-or-through the fill price within `tape_check.window_secs` (60) of booking, and the daily report adds a tape-confirmed P&L column per class. "The quote persisted" is weaker evidence than "someone actually traded there" — this is the strongest fill validator available without placing orders, but still an upper bound: the print may be the very liquidity the flag counted on (someone else took it), so tape-confirmed never means fill-certain in go-live math. Kalshi candlestick history (verify per rule 3) may additionally backfill markout horizons for Kalshi legs, fixing sparse +30s coverage without the WebSocket.

## Trust split and headline definition

Every bet carries `trusted` (venue-pair whitelisted; single-venue kinds per §12) and `lane ∈ {clean, review, invalid}` from the eligibility predicate. **Headline daily number = trusted + sports + pregame + clean, only.** Headline P&L is **gross of carry** — capital-lockup cost is a projection-layer charge (§13.4); any externally cited ROI must say so (v3.4). Non-sports, `live=true`, AI-matched, micro, and review rows are each tracked in their own always-visible sections, never blended.

## Tracker test vector (Phase 6 acceptance)

Simulated fixture: poll1 flags ARB (S=0.96) + EV (edge 3%, ask 0.50); poll2 ARB persists @0.97, EV gone; poll3 EV reappears @0.49. Expected: ARB one bet, optimistic pnl = 1/0.96 − 1 ≈ $0.041667, confirmed = 1/0.97 − 1 ≈ $0.030928; EV two rows (second has `reappearance_of` set), first has no `confirmed` bet_events row (a "missed"); event settles YES → EV row 1 optimistic pnl = (1 − 0.5175)/0.5175, all asserted to the cent with Kalshi fees applied (paper mode, §6.5). The fixture's test config sets `ev_enabled: true` — the freeze is a production default, not a removed code path.

## Exit engine — the SELL side (v3.5, `exit` config)

Booking is the simulated **BUY**: at flag time the bet fills at ask + taker fee (that is what `pnl_optimistic` already encodes). This section defines the simulated **SELL**. Design constraint first: "sell at maximum profit" is not implementable ex ante — the maximum is only knowable after the fact — so the engine runs three **pre-registered exit policies in parallel as read-time counterfactual columns**, judged against each other by data. No new tables, no ledger writes: every policy is a pure function of the quote series already persisted at 60s cadence, computed by `metrics.py`.

**Sell price basis (binding):** a sell crosses the spread and pays the venue's taker fee on the exit trade: `exit_value_per_contract = bid − leg_fee_cost(venue, category, bid, 1)`. Selling at mid or ask is fiction; Kalshi and Polymarket both charge the quadratic taker fee on the closing trade.

**Policies** (per open resolution-settled bet, evaluated at every stored poll):

- **P0 `hold`** — settle at resolution. The baseline and the current behavior; for locked kinds (ARB/XSPREAD) P0 is optimal by construction and the engine does not apply (an optional both-legs-unwind counterfactual may be logged, never booked).
- **P1 `take_profit`** — exit at the first poll where `(exit_value − c) ≥ exit.capture_fraction × (p̂_entry − c)` — i.e., sell once the position has banked the configured fraction (default 0.8) of the flagged edge. This is the "sell when most of the profit is made" rule stated honestly: it captures the profit that has *materialized in the price*, without resolution risk, and frees the capital (which feeds the §13.4 annualized-edge math).
- **P2 `trailing`** — track peak unrealized P&L on mids; arm only after the peak reaches `exit.min_peak_edge_fraction` (0.5) of the flagged edge; exit when unrealized P&L falls `exit.trail_fraction` (0.4) below the peak. This is the closest implementable approximation to "sell at the top": it rides the move and sells on the turn.

**Vector (Phase 6 acceptance, exact `Decimal`):** EV bet, entry ask 0.50, Kalshi fee 0.0175 → c = 0.5175, p̂ = 0.55, flagged edge 0.0325, stake $1 → 1.932367 contracts. Later poll shows bid 0.58: sell fee = 0.07 × 0.58 × 0.42 = 0.017052 → exit_value = 0.562948; trigger check: 0.562948 − 0.5175 = 0.045448 ≥ 0.8 × 0.0325 = 0.026 → P1 exits; `pnl_exit_P1 = 1.932367 × 0.045448 = $0.087822`. P0 for the same bet remains whatever resolution pays — both columns print side by side.

**Honesty rules:** the daily report adds, per class × policy: realized P&L, capital-days used, annualized edge, and **peak-captured %** (realized ÷ maximum unrealized over the bet's life — the direct measure of how close each policy gets to the unknowable maximum). A policy is promoted to default only per `exit.promote_requires`: ≥ 100 settled bets, cluster-bootstrap CI on (policy − P0) excluding zero, replicated in a second out-of-sample month — the same evidentiary bar as everything else. Exits are evaluated from quotes at the stored cadence, so exit fills inherit the same cadence-conditionality caveat as every other number (§18). Live order placement remains forbidden (rule 1): this engine defines what the live SELL executor would do *if* the deployment gates (§13.4, addendum) ever pass, and measures on paper whether its rules actually beat holding.

# Settlement and reconciliation

## Settling

ARB settles at entry (locked P&L) — *after* passing the eligibility predicate and timing guards. EV/NEGRISK settle at event resolution: MLB via the MLB Stats API (primary; free, unlimited history), other sports via Odds API scores (`daysFrom=3`), non-sports manually via `arb settle --manual EVENT RESULT`. Settlement never touches `flat_bets`: each resolution appends one terminal `settled` bet_events row — rule 9's mechanism, not an exception to it. Odds API settlement requires `completed: true` — the scores payload includes live in-progress scores, and settling on one re-opens bug class A3 through a new door. VOID results (pushes, no-contests) book P&L = $0 with stake returned; whether fees refund is a per-venue dated fact recorded beside the fee rates (verify once per venue); VOID rows are excluded from win-rate denominators. PARTIAL results (v3.4) book `pnl = stake × (fraction/cost − 1)` per leg — derivation: stake `s` buys `s/c` contracts, each paying `fraction f` dollars → payout `s·f/c` [dollars] — and join VOID outside win-rate denominators. P&L attributes to the EST day the bet was placed, so past daily rows get restated as pending bets settle — restated days are marked `*` in reports.

**Settle-eligibility (fix for V1 bug A1 — the 314/314 forbidden-arbs bug):** the settle query selects open bets through the same eligibility predicate as everything else. REVIEW bets still settle — into the review lane, `result='RESOLVED_REVIEW'`, never touching clean/headline P&L. A regression test settles a fixture with one OK and one REVIEW arb and asserts only the OK row lands actionable.

**Timing guards (fix for V1 bug A3 — 85/314 arbs placed after the event):** at settle, compare `placed_at` to the event start/final: placed after → append `INVALID_AFTER_FINAL`, excluded from clean. Defense in depth with the detection-time guard (§8.4). A future-start guard refuses to settle an event that hasn't started.

## Reconciliation (per-venue resolvers)

Settlement by scores is provisional until each leg's **venue** confirms resolution. Implement one resolver per venue, composed by `RESOLVERS[venue]`, each returning `WON|LOST|VOID|PARTIAL(payout_fraction)|None` (v3.4 — UMA can settle ambiguous markets at fractional prices, e.g. 50/50; a fractional settlement is none of WON/LOST/VOID and mis-booking it as any of them mis-states P&L):

- Kalshi: market result via the public market endpoint.
- Polymarket: conditionId / UMA resolution via public Gamma/CLOB reads — returns None until resolution is final past the UMA dispute window (disputes flip outcomes; provisional-then-flip must be exceptional, not routine).
- SX: `/markets` settled status + the leg's outcome side.

Reconcile runs automatically after every settle cycle. Only mark a bet unresolved if *its* venue's resolver returns None (V1 skipped a whole bet when any leg was unresolved, and only a Kalshi resolver existed — reconciliation was a structural no-op for 100% of a book whose every bet had a non-Kalshi leg). Until reconciled, realized P&L is labeled `provisional` in every surface. Corrections append rows; a reconciled mismatch on a trusted pair reverts the pair to REVIEW immediately.

## Start-time truth hierarchy

`start_grade = verified` (MLB Stats schedule, which outranks all venue times) > `nominal` (venue-listed) > `absent`. A bet created from a `nominal` start where a verified source exists is a bug; `absent` is never actionable. This plus the no-`close_time`-fallback rule kills the whole family of V1 timing corruptions.

## Settlement matrix (the gate to any trusted path)

`data/settlement_matrix.yaml`: per sport x scenario (tennis retirement; MLB extra innings, suspension, doubleheaders; soccer extra time/penalties/abandonment; MMA no-contest and card changes — the MMA parity check found a real Kalshi-vs-Polymarket divergence on cancellation handling), each cell GREEN/AMBER/RED with rulebook-section citations. The trusted gate requires every cell for a sport GREEN before the venue pair can be whitelisted for it; a reconcile retro-check flips cells from lived settlements. No vibes-based whitelisting.

# Eligibility: one predicate, one lane definition

`arb/eligibility.py` exposes a single function:

```python
def classify(bet_or_opp, config) -> tuple[bool, str, str]:
    """Returns (eligible, reason, lane) with lane in {'clean','review','invalid'}."""
```

It encodes, in one place: trusted venue-pair membership; settlement-matrix GREEN requirement; pregame vs. in-play; start-grade; depth floor; skew/adjusted-edge; quality flags; category segregation (sports vs. non-sports vs. AI-matched vs. micro vs. live). Detection, tracker, settle, reconcile, capacity, exposure, and report all import it — none reimplements any part of it. Single-venue kinds (NEGRISK, POLITICAL) have no venue pair: they are trusted-eligible by construction (single venue, single oracle) once their §18.4 "real if" gate and a registered settlement path exist — and, for NEGRISK, only for baskets that pass the §8.4 exhaustiveness proof: "single venue, single oracle" is not "provably covering", and the proof is the covering-set requirement's single-venue twin. They occupy the clean lane subject to the same depth/timing/quality guards; their `venue_pair` column records `<venue>:self` so per-pair breakdowns never blend them with cross-venue rows. The POLITICAL lane scans only venues whose YES and NO books are independent instruments (Polymarket CLOB tokens): on Kalshi `no_ask = 1 − yes_bid` (§6.1), so a Kalshi intra-market "hit" is a crossed-book feed artifact — it writes a `crossed-book` quarantine diagnostic, never a bet. Cross-venue kinds are `XSPREAD` (v3.5): for them the predicate additionally computes `net_spread_edge = gross_edge − Σ fees − (3/n) × mismatch_pricing.lgm` from the pair's Resolution Equivalence Certificate (n = mismatch-free reconciled positions, expiring on any `rules_sha` change; addendum §1; n = 0 — no certificate or expired — is ineligible outright, no division performed) and can mark clean only if positive — which, with `deployment.xspread_autobook: false` and the §2 arithmetic, is currently never. Cross-venue same-oracle kinds (BTC) stay REVIEW until the oracle-agreement study passes. Frozen tests: a REVIEW arb can never reach clean/headline P&L (fixture test), and a grep-test proves the logic exists in exactly one module. This single change retires V1's three parallel notions of "clean" (trusted flag / quality_flag / status), which were the root cause of both incidents' guard gaps.

The BTC lane's V1 gap belongs here too: **no opportunity class may book bets without a registered settlement path.** A `kind` without a settler is a config error at startup, not a permanent-OPEN surprise later.


# Operational discipline (practicality)

## Cadences and loops

Two cadences. Primary `watch`: every `watch_interval_secs` (60s) across all three venues (keyless, unlimited). Secondary `scan` (4x/day at `scan_times`, only if `consensus_enabled`): refreshes sportsbook consensus. `watch` never calls The Odds API; EV math uses stored consensus, annotated `stale-consensus` beyond `consensus_max_age_hours` (8h; pregame flags quarantined beyond 3h — a pregame line vs. an 8-hour-old consensus is "venue vs. stale line"). **Booking rule (v3.4):** a would-be EV bet whose *first* sighting exceeds the pregame freshness bound books nothing — it writes `left_on_table` (reason `stale-consensus`); the bet row is created only at the first fresh-consensus sighting. Booking-then-quarantining is forbidden by the system's own arithmetic: bets book at first sighting, position-dedup absorbs every later sighting, and quality flags are immutable (rule 9) — so a stale first sighting would quarantine the position *for life* even when a fresh sighting follows 20 minutes later. With the old scan grid this hit the two modal MLB start windows: scans at 06:15/10:00/15:00/19:00 with a 3h bound leave freshness dead zones at 09:15–10:00, 13:00–15:00, 18:00–19:00, and 22:00–06:15 — squarely over the ~13:05–13:40 and ~18:40–19:10 ET start clusters. If `scan_breaks: true`, re-scan during scheduled pauses (NBA halftime ~48min after start, soccer ~45min; configurable); break-window quotes get `live=true` and never blend into headline numbers.

**Fetch discipline:** the three venue fetches in a cycle run concurrently (threads), each under `venue_fetch_timeout_secs` (15); 429/5xx get exponential backoff with jitter, at most `venue_max_retries` (2) per cycle; a venue that still fails marks the poll partial for that venue (§10.2). Sequential venue fetching is forbidden — it self-inflicts inter-leg skew against the 10s actionable bound.

**Cadence honesty:** the daily report prints planned vs. actual poll interval (mean and p95), plus the achieved inter-venue fetch-skew distribution (p50/p95) so the 10s actionable bound is calibrated to the instrument, not aspiration. V1's loop believed it ran at 60s while actually running at ~140s; the p95 cycle time in the report is the loop's real spec, and every timing claim keys off it.

## Supervisor and heartbeats

All loops run as subprocesses of one `arb supervisor`:

- Children are **one-shot** (`watch --once`); the supervisor relaunches each on exit. A second supervisor copy refuses to double-run (lock dir `.locks/`).
- Every child writes a heartbeat row `(process, beat_at, head_sha, pid)` at cycle end. The `head_sha` column is the structural fix for the dirty-tree failure class: any process beating from a sha ≠ committed HEAD is visible instantly.
- Heartbeat staleness alone must NOT trigger a restart (a healthy multi-minute live cycle beats only at the end — V2 learned this by killing healthy children in a loop for three days). Force-restart only a child alive past its runtime cap — `max_child_runtime_secs` is a **per-process map** (v3.3): a settle+reconcile cycle over a large open book legitimately exceeds the 600s watch default, and killing it mid-reconcile re-creates the healthy-child-killing failure class.
- `data/stop-all` (a flag file) halts every loop cleanly; only the documented restart procedure removes it. The flag is checked **between** cycles only — a cycle in flight always completes its atomic write. The supervisor also owns the clock: `scan_times`, `settle_time`, and `daily_report_time` fire from the supervisor loop (stdlib time arithmetic; the `schedule` dependency is dropped).

## Host survival (the Jul 4–6 outage lesson)

The machine sleeping is a proven zero-data failure mode that heartbeats do not catch (the supervisor kept beating while DNS failed). Requirements:

- The documented start command wraps the supervisor in `caffeinate -i` (macOS) or the platform equivalent; better, install a launchd/systemd unit with `KeepAlive`. `caffeinate -i` does not survive lid-close or battery — on a laptop, set `pmset` accordingly or run headless; the start doc must say which.
- **Stale-data alarm:** `arb status` and the daily report both alarm when the newest persisted quote is older than `data_stale_alarm_secs` (900). An invisible outage is the failure; the alarm makes it loud.
- `status.sh` / `arb status` must model the supervisor's process semantics: fresh beat OR live child pid → ALIVE; stale beat + live supervisor lock → STALE; DEAD only with no supervisor. (Keying status on child pids reads a healthy one-shot loop as DEAD.)
- **Backup:** the DB is the entire evidentiary record and currently lives on one machine with a proven sleep habit. `sqlite3 .backup` (or `VACUUM INTO`) daily at `db_backup.time` to `db_backup.dest` (second disk or synced folder); the daily report prints backup age beside the stale-data alarm. Daily report `.md` files are committed to git — versioned evidence (CSVs stay ignored).
- **Clock and logs:** `arb status` checks NTP offset and alarms past `clock_skew_alarm_secs` (5) — every timing guard compares venue timestamps to local now. Logs rotate daily, retained `log_retention_days` (90): the forensics tool is grepping these.
- **Weekly live shape-check:** one scheduled fetch per venue per week validates response *schema* (not values) against the fixtures; drift alarms instead of surprising a parser at runtime.
- **Canary (instrument calibration):** monthly (`canary.day_of_month`), inject a synthetic fixture — event, quotes, resolution all predetermined — through the full live pipeline in a tagged `canary` lane and assert the reported P&L matches the precomputed value to the cent. A canary mismatch is a sev-1. Labs run known standards through the assay; so does this instrument (the V1/V2 incidents were compositions failing while every unit test passed).

## Sizing, capacity, exposure (projections only)

Flat-$1 is the only headline. Everything else is a parallel projection and must say so:

- **Capacity** reports `INSUFFICIENT_DEPTH_DATA` until ≥ `min_ladder_fraction` (0.5) of feasible arbs carry real multi-level ladders. Top-of-book-only capacity numbers are fiction (V1's "$72,209 summed profit" stacked zero ladders, 4.5x row duplication, and 99% REVIEW rows). Aggregate by `position_key`, filter to trusted, display the captured-ladder fraction, and haircut displayed depth by `fillable_fraction` for sizing. Go-live projections also subtract `fixed_cost_per_fill_usd` per venue (gas/redemption/withdrawal amortization — at $50-depth, 1%-edge scale, fixed costs flip signs) and note that after-tax edge is the real hurdle: wins are taxed while cross-venue hedge losses may not offset. Cross-venue projections also carry the USD:USDC settlement basis (Kalshi settles USD; Polymarket/SX settle USDC) — ≈0 in calm markets, widest exactly in stress (v3.4).
- **Exposure** runs inside the scan: refuse to promote a new actionable flag when total or per-venue committed stake would exceed caps; charge capital to the later venue's settlement (start + sport duration + venue payout lag), not to event start; tag locks below `capital_charge_annual` (5%/yr pro-rated) as `uneconomic`.
- **Kelly** (binary-market `f* = (p − c)/(1 − c)` with `c` fee-inclusive — algebraically identical to `(pb−q)/b` at `b=(1−c)/c`, derivation in addendum §3; quarter-Kelly, hard-capped at 5% of bankroll per bet) and depth-aware ARB scaling are projection columns gated behind their own pre-registered enablement rules (§18.4). Sizing caps (v3.5): `min(quarter-Kelly, visible_depth/5, q*)` where `q*` is the largest size whose ladder-VWAP slippage ≤ `slippage.max_edge_fraction` (0.20) of net edge — projection fills price at VWAP, never top-of-book. Portfolio caps: per event 5%, per sport 10%, same-day same-league totals 15% of bankroll. Deployment additionally requires `annualized_edge = (1+net_edge)^(365/lockup_days) / (1+risk_free_rate) − 1 > deployment.annualized_hurdle` (0.20; geometric excess return — subtracting the rate arithmetically overstates the excess) and a passing sport-day-block Monte Carlo ruin report (`P(bankroll < 0.5) ≤ 0.01` at quarter-Kelly over ≥1,000 resampled bets; `INSUFFICIENT_HISTORY` below 300 settled clean bets). Addendum §3–§6.

# UX

## CLI

`python -m arb <command>` with `rich` tables. Commands:

| Command | Purpose |
|---|---|
| `watch [--once] [--dry-run]` | primary loop: fetch, match, detect, track, persist |
| `scan` | Odds API consensus refresh (no-op if disabled) |
| `settle [--manual EVENT RESULT]` | resolve pending bets; auto-runs reconcile + check-invariants; `--manual` echoes the parsed event+result and requires `-y` (appends immutable rows — a typo is forever) |
| `report [--daily DATE]` | recompute any day idempotently from `flat_bets` |
| `status` | process states, data freshness alarm, credits, P&L snapshot |
| `check-invariants` | the five ledger invariants; silent when clean |
| `regrade [--db PATH]` | re-score consensus vs. venue mids on any archive (read-only) |
| `supervisor` | own all loops; the only sanctioned way to run them |
| `negrisk-scan`, `verify`, `hot`, `btc`, `political`, `nonsports` | optional lanes, supervisor-owned |

Design rules learned the hard way: every command is safe to run twice (idempotent or refuses); every command prints which DB and HEAD sha it ran against; destructive-looking words never appear (there is nothing destructive to do); one `metrics.py` computes every number so the terminal, the daily file, and any dashboard can never disagree (V1 had three surfaces computing P&L independently — they diverged). One exception to single-implementation: a standalone ~30-line SQL script (importing nothing from `arb/`) independently recomputes the headline number from raw `flat_bets`; CI asserts it matches `metrics.py` on a fixture DB — dual implementation is reserved for the one number the project exists to produce.

## The daily report (the product)

Auto-written at 23:59 America/New_York as `reports/daily/YYYY-MM-DD.csv` (one row per bet) + `.md` (summary) + terminal print. Contents:

- Per class x fill-assumption (optimistic / confirmed / markout) x trust lane: bets, $ staked, settled P&L, pending, win rate, ROI.
- Breakdowns by venue-pair (never pooled), sport, category (sports / non-sports / AI-matched separate), edge bucket (0–1%, 1–2%, 2–5%, >5% — the threshold-tuning table).
- Biggest win / biggest loss / largest unrealized pending.
- **Left on the table:** count + summed edge of flags killed by each filter, by reason.
- Flagged edge vs. realized edge per class (the honesty metric).
- Cadence line: planned vs. actual interval, p95 cycle time.
- Per-venue fetch success rate by hour (outages correlate with volatile moments; invisible downtime censors every lifetime and vanish-rate statistic).
- Premise monitor: rolling consensus-vs-venue-mid Brier, computed both as-traded and same-timestamp (§18.3).
- Data-freshness alarm state; stale-fee badges; restated-day markers (`*`); cumulative since inception under both fill assumptions.

## Status and control

`arb status` (and thin `status.sh` wrapper): supervisor/child states via heartbeats + lock, newest-quote age with alarm, Odds API credits remaining, today's flags by lane, open bet counts, last settle/reconcile result. Optional read-only local dashboard (single HTML page reading `metrics.py` output). Keep the control surface minimal: flag files (`data/stop-all`, `data/stop-<lane>`) for halts; config edits + supervisor restart for everything else. Web server, if kept, swallows client-disconnect tracebacks (log one line — traceback greps are the outage forensics tool and must stay clean).

# Config reference (`config.yaml` — complete, annotated)

Every value the system reads. Defaults are the V2-hardened values; comments are binding documentation.

```yaml
timezone: America/New_York          # all daily boundaries EST/EDT
bankroll_usd: 1000
max_stake_pct: 0.10
ev_threshold: 0.02                  # display gate only; ev.e_min is the booking floor if re-opened
divergence_threshold: 0.03
push_pricing: false                 # optional Poisson push-prob scaling of integer-line edges (SS8.3)
arb_stale_warn: 0.04                # ROI above this books REVIEW automatically for EVERY locked kind
                                    #   (ARB, NEGRISK, POLITICAL, BTC) — stale quote or mismatch, not edge
max_leg_skew_secs: 60               # outer recording bound (actionable bound is execution.*)
max_start_drift_mins: 30            # venues disagreeing on start = match suspect
match_suspect_prob_diff: 0.15       # same outcome priced >15pts apart = matching error
capital_charge_annual: 0.05         # locks below T-bill-ish carry are 'uneconomic'
sport_typical_duration_hours: { baseball_mlb: 3.5, basketball_nba: 2.5,
                                soccer: 2.0, tennis: 2.5, mma: 1.0 }
venue_settlement_lag_hours: { kalshi: 24, polymarket: 48, sx: 24 }  # verify empirically
min_books_for_consensus: 3
min_books_for_consensus_totals: 2   # totals are line-fragmented
consensus_outlier_points: 0.10      # optional outlier drop: book >10pts from cross-book median excluded
devig_method: power                 # coin-flip default (n=30 bake-off is noise); re-run at n>=200
sharp_books: [lowvig, betonlineag]  # betus removed — soft book, not sharp
weight_sharp_books: false           # measurement-parallel until it beats plain median on CLV
consensus_anchor: { enabled: false, book: pinnacle, region: eu, markets: h2h }
                                    # single sharp anchor experiment (SS18.3); h2h ONLY:
                                    #   420 credits/mo; h2h+totals = 540 > 500 budget (SS6.4)
min_executable_usd: 50              # the only global per-leg depth floor
micro:                              # thin-book EV lane, SUBORDINATE to ev_enabled:
  enabled: true                     #   while ev_enabled is false, micro books nothing
                                    #   (would-be flags -> left_on_table)
  min_depth_usd: 5
  max_stake_usd: 25
  e_min: 0.08                       # edge floor at full depth...
  e_min_thin: 0.15                  # ...rising linearly as depth thins: interpolate from
                                    #   (min_executable_usd -> e_min) down to
                                    #   (micro.min_depth_usd -> e_min_thin) (pinned v3.4)
  assume_thin_venues: [sx]
  max_event_exposure_usd: 50
  max_venue_exposure_usd: 100
max_days_to_settlement: 14          # booking gate for resolution-settled bets; farther -> left_on_table
nonsports_max_days: 7               # resolution-settled non-sports only; locked-at-entry intra-market
                                    #   arbs (political lane) are EXEMPT — political markets run months
sports: [baseball_mlb, basketball_nba, soccer, tennis, mma]   # add americanfootball_nfl by 2026-09-01 — activity gating is blind to sports missing here
unreliable_start_venues: { polymarket: [tennis] }
activity_lookahead_hours: 48        # sport ACTIVE iff a venue lists a fixture in window
consensus_sports: [baseball_mlb]    # add tournament keys only while they run
odds_api_sport_aliases: { soccer_fifa_world_cup: soccer,
                          tennis_atp_wimbledon: tennis,
                          tennis_wta_wimbledon: tennis,
                          mma_mixed_martial_arts: mma }
odds_api_markets: { default: "h2h,totals",
                    tennis_atp_wimbledon: h2h, tennis_wta_wimbledon: h2h,
                    mma_mixed_martial_arts: h2h }
scan_breaks: true
break_windows: { basketball_nba: [48, 15], soccer: [45, 15] }  # [offset-mins-after-start, duration-mins]
                                    # NBA 48 is a placeholder — real halftime starts ~60-70
                                    #   wall-clock mins after tip; calibrate from stored quotes
watch_interval_secs: 60
consensus_enabled: true
ev_enabled: false                   # FROZEN by Phase A verdict; see PREREGISTRATION
consensus_max_age_hours: 8
consensus_pregame_max_age_hours: 3
ev:
  e_min: 0.05                       # booking floor when re-opened (display gate: ev_threshold)
  clv_window_mins: 30               # closing mid = last consensus within this window of start
  kelly_fraction: 0.25
  edge_suspect_above: 0.25          # above = quarantine as data error
  max_edge: 0.15                    # above = record quote, book nothing
  require_exact_match: [tennis, mma]  # golf removed — not a supported sport anywhere in the system
  enforce_depth_floor: true
  reject_one_sided_books: true
scan_times: ["06:15", "10:30", "13:30", "18:30"]
                                    # v3.4: retimed so the 3h freshness windows cover modal
                                    #   MLB starts (13:05-13:40, 18:40-19:10 ET); the old
                                    #   10:00/15:00/19:00 grid left 13:00-15:00 and
                                    #   18:00-19:00 dead (SS13.1 booking rule)
daily_report_time: "23:59"
settle_time: "09:30"
settle_days_from: 3                 # Odds API scores max lookback
settle_tripwire_secs: 60            # instant-settle = knowable at placement
tracker:
  terminal_mute_grace_mins: 180
  max_reappearances: 10             # observability bound, logs only
mlbstats_enabled: true
auto_fill: false                    # permanently false; flat_bets is the executor
trusted_pairs: []                   # empty until the SS18.2 gate clears — weeks by construction
fuzzy_match_threshold: 92
fee_stale_days: 30                  # fees are dated facts; stale = badge, never halt
kalshi:      { fee_rate: 0.07, maker_fee_rate: 0.0175,   # maker = 25% of taker, NOT zero
               source_url: "https://kalshi.com/docs/kalshi-fee-schedule.pdf",
               confirmed_on: 2026-07-02 }
polymarket:  { sports_fee_rate: 0.03, crypto_fee_rate: 0.07, politics_fee_rate: 0.04,
               econ_weather_fee_rate: 0.05, geopolitics_fee_rate: 0.0, fee_rate: 0.05,
               source_url: "https://docs.polymarket.com/trading/fees",
               confirmed_on: 2026-07-02 }   # QUADRATIC per category; makers 0
sx:          { fee_rate: 0.0, win_fee_rate: 0.0,   # singles free; 5% winning PARLAYS only
               source_url: "https://help.sx.bet/en/articles/2798017-sx-bet-fees",
               confirmed_on: 2026-07-02 }
execution:
  taker_price_buffer: 0.005         # half-cent per contract vs latency/queue risk
  slippage_model: flat              # 'ladder' is a versioned change to every cost vector
  fillable_fraction: 1.0            # flip toward 0.5 once live fills calibrate it
  fixed_cost_per_fill_usd: { kalshi: 0.0, polymarket: 0.02, sx: 0.02 }
                                    # gas/redemption/withdrawal amortization; projections only — verify
  arb_size_by_ladder: false         # SS18.4 gate
  arb_size_safety_frac: 0.5
  min_ladder_fraction: 0.5          # capacity says UNKNOWN below this
  max_actionable_skew_secs: 10
  venue_fetch_timeout_secs: 15      # per-venue budget; venue fetches run concurrently
  venue_max_retries: 2              # 429/5xx backoff with jitter, then poll partial for venue
  min_adj_edge_points: 0.002        # NB: "points" = probability FRACTION (0.002 = 0.2pts),
                                    #   never percentage points — unit bug = 100x error
  line_velocity_points_per_sec: { default: 0.001, baseball_mlb: 0.0008,
                                  basketball_nba: 0.0015 }
                                    # fractions/sec; pre-registered PLACEHOLDERS — re-estimate
                                    #   from the lead-lag lane's stored per-event mid changes
  doubleheader_cluster_tol_secs: 7200
odds_api: { region: us, warn_below_credits: 100, abort_below_credits: 50 }
negrisk:
  enabled: true
  min_edge: 0.006                   # dry-run patch A1: MUST exceed p_tail/(1-p_tail) where
                                    #   p_tail = 1 - exhaustive_proof.mids_sum_min, or the
                                    #   proof tolerance admits worst-case-NEGATIVE baskets
                                    #   into the headline; loader enforces the inequality
  scan_no_side: true                # also flag sum(NO asks) < N-1 baskets
  kalshi_events: true               # also scan Kalshi event_ticker groups of mutually exclusive markets
  require_exhaustive_exclusive: true  # enforced by the SS8.4 proof below, never assumed
  exhaustive_proof:
    require_venue_flag: true          # Polymarket negRisk flag / Kalshi event mutually_exclusive (verify per rule 3)
    mids_sum_min: 0.995               # dry-run patch A1: tolerated tail (1 - this) must sit
                                      #   below min_edge; 0.98 tolerated a 2% tail against a
                                      #   0.5pt edge floor = negative-EV baskets booked clean
    mids_sum_max: 1.10                # above this = overlapping outcomes or stale books -> REVIEW
    max_half_spread_sum: 0.005        # dry-run patch A2: proof invalid if summed half-spreads
                                      #   exceed the window slack (1 - mids_sum_min) — noisy
                                      #   mids can mask a tail larger than the tolerance
    require_two_sided_all_outcomes: true  # any listed outcome without a live two-sided book
                                          #   -> unproven-exhaustive (v3.4)
  max_outcomes: 30
  scan_limit: 200
  scan_pages: 3                     # paginate; V1's page cap hid most of the surface
  min_executable_usd: 5             # single-venue: thin baskets worth measuring
btc_arb:
  enabled: false                    # enable only WITH its settlement path + oracle-agreement study
  window_match_secs: 5
  max_window_minutes: 15
  trusted: false
parlay: { enabled: false, divergence_threshold: 0.05, min_liquidity_usd: 500,
          sx_win_fee_rate: 0.05 }
ai_matcher:
  enabled: false                    # enable after ground-truth set exists (SS18.5)
  model: claude-haiku-4-5
  min_confidence: 0.85
  max_new_pairs_per_day: 100
  max_rules_chars: 8000
  paper_fill: true
  paper_stake_usd: 1
nonsports:
  watch_interval_secs: 300
  candidate_threshold: 85
  kalshi_categories: ["Politics", "Economics", "Climate and Weather"]
  polymarket_tags: [politics, economy, weather]
verify: { poll_secs: 10, max_bet_age_secs: 900 }
hot:    { poll_secs: 2, check_interval_secs: 1.0, watch_duration_secs: 900,
          dead_checks: 3, max_watchers: 3 }   # 900s: 120s right-censored every lifetime
scoring:                            # consumed by SS18.3 segment gating (regrade/report)
                                    # v3.3: the weighted composite score is DROPPED — an
                                    #   arbitrary 0.40/0.25/0.20/0.15 blend is researcher
                                    #   degrees of freedom; segments pass or fail the SS18.3
                                    #   hard gates (Wilson, CLV, ECE, n) independently
  score_min_n: 30
  block_below: 30
  recent_window: 10
  calibration_gate: true            # ECE gate blocks a segment even if ROI passes
  ece_estimator: debiased           # v3.4: raw ECE has a noise floor — under PERFECT
                                    #   calibration E[ECE] ~= sqrt(2/pi) * sum_b w_b *
                                    #   sqrt(p_b(1-p_b)/n_b) ~= 0.06 at n=200 / 5 bins,
                                    #   nearly the old 0.10 gate; gate on ECE_raw - E0
  ece_max: 0.05                     # on the DEBIASED estimator (~= the old raw-0.10 intent)
  ece_bins: 5                       # quantile bins; 10 equal-width bins at n=200 flap
sizing: { kelly_fraction: 0.25, max_kelly_fraction_of_bankroll: 0.05 }
validation:
  ev_filter: { rule_id: H-EVFILTER, kind: EV, min_cost: 0.40, max_cost: 0.75,
               max_flagged_edge: 0.02, target_n: 190, halfway_n: 95,
               kill_consecutive_losses: 10 }
supervisor:
  processes: { watch: true }        # add lanes deliberately, one at a time
  commands:  { watch: "watch --once" }
  max_child_runtime_secs: { default: 600, settle: 1800 }  # per-process; settle+reconcile runs long
data_stale_alarm_secs: 900
clock_skew_alarm_secs: 5            # status alarms if NTP offset exceeds this
log_retention_days: 90              # rotate daily; forensics greps these
db_backup: { time: "04:10", dest: "backups/" }   # sqlite3 .backup daily; report prints backup age
                                    # dest MUST be a different disk or synced folder (SS13.3);
                                    #   a same-volume dest is a startup WARNING (v3.4)
canary: { enabled: true, day_of_month: 1 }       # monthly synthetic end-to-end fixture, tagged lane
tape_check: { enabled: true, window_secs: 60 }   # fill tape_confirmed only if venue printed a trade
                                    #   at-or-through the fill price in-window (verify endpoints, rule 3)
zombie_spread_min: 0.90             # volume_24h==0 AND (ask-bid)>=this -> zombie tag, non-actionable
risk_free_rate: { value: 0.043, source_url: "TODO-13wk-T-bill", confirmed_on: 2026-07-07 }
                                    # dated fact like fees [VERIFY at build] (v3.5)
deployment:                         # capital-deployment layer (addendum SS2-SS6); paper unaffected
  annualized_hurdle: 0.20
  xspread_autobook: false           # STRUCTURAL: floor max(2F,0.5%) > ceiling F+0.5% for all F>0.5%
  auto_book_min_net_edge: "max(2*total_fees, 0.005)"   # same-venue ARB only
  leg_failure_suspend_rate: 0.20
  fok_window_secs: 5
slippage: { max_edge_fraction: 0.20, depth_divisor: 5 }
mc_sim: { n_bets: 1000, block: sport_day, ruin_threshold: 0.5, max_ruin_prob: 0.01,
          min_history_bets: 300 }
mismatch_pricing: { lgm: 1.0, rule_of_three: true }    # p_upper = 3/n mismatch-free positions
caps: { per_event_frac: 0.05, per_sport_frac: 0.10, per_day_totals_frac: 0.15 }
kind_taxonomy: { cross_venue: XSPREAD, same_venue_locked: ARB }   # RULE 1 (v3.5)
exit:                               # SELL engine (SS10.8): read-time counterfactual columns,
  enabled: true                     #   pure function of stored quotes — no ledger writes
  policies: [hold, take_profit, trailing]
  capture_fraction: 0.8             # P1: sell once 80% of the flagged edge is banked in price
  trail_fraction: 0.4               # P2: sell on a 40% drawdown from peak unrealized P&L
  min_peak_edge_fraction: 0.5       # P2 arms only after peak >= 50% of flagged edge
  exit_price_basis: bid_minus_fee   # sells cross the spread AND pay the taker fee
  promote_requires: { min_n: 100, ci_excludes_zero: true, replication_month: true }
```

# Build phases (for a Claude Code executor)

Implement strictly in order. Each phase ends with `pytest` green plus the named acceptance command, whose output must be shown. Do not start a phase until the previous one's acceptance passes. Commit per phase (or per finding within a phase) with evidence pasted into the commit message. Modules stay under ~300 lines. Python 3.11+; dependencies pinned to exact versions in `requirements.txt`: `requests, rich, rapidfuzz, python-dotenv, pyyaml, pytest` (`anthropic` is an optional extra for the AI matcher only — never for market data; `schedule` is dropped — the supervisor owns the clock); stdlib `sqlite3`. `ODDS_API_KEY` from `.env` (git-ignored; ship `.env.example`).

| Phase | Build | Acceptance |
|---|---|---|
| 1. Skeleton | `config.py` (typed loader, no getattr-with-default fallbacks duplicating loader defaults, unknown keys are startup errors), `models.py`, `schema.py`/`store.py`/`migrations.py`, CLI stub, hygiene grep-tests | `arb report` prints an empty frame; DB file created with full schema; `pytest` green |
| 2. Math | `pricing.py` + `venues/fees.py`: every §7 vector exact in `Decimal`; de-vig x3 methods; fee models incl. Polymarket quadratic + negRisk regression vector | all vectors pass to the cent/6th decimal |
| 3. Venue adapters | fixtures first (record one real response per venue per market type), then parsers; SX inversion + depth tests; Kalshi empty-book + no-close_time-fallback tests; Polymarket CLOB-only + full-book ladder; `left_on_table` plumbing | adapters parse fixtures; one live `watch --once --dry-run` prints per-venue quote counts |
| 4. Matching | aliases, event keys, doubleheader clustering, ladder-aware totals pairing, detection guards, non-sports manual map | fixture: same MLB game on 3 venues → one event_key; totals strike-ladder pairing test; doubleheader fixture never cross-pairs |
| 5. Detection + eligibility | `arbitrage.py`, `negrisk.py`, `eligibility.py` (with T-ELIG fixture tests + grep-test), display | live `watch --once` end-to-end; quotes persisted; a REVIEW arb provably cannot reach the clean lane |
| 6. Tracker + settle + report | `tracker.py` (§10 vector), `settlement.py` + tripwire + timing guards, `reconcile.py` with all three resolvers, `invariants.py`, `metrics.py`, daily writer | §10.7 vector passes; simulated 3-poll fixture produces the exact CSV; `check-invariants` silent; run `watch` 30 min live then `report --daily` renders non-empty poll/quote counts and exercises the zero-flag path (flags cannot be guaranteed in any window) |
| 7. Ops | `heartbeat.py` supervisor, stale-data alarm, `status`, caffeinate/launchd + pmset docs, markouts persist hook, db backup job, log rotation, canary harness, weekly live shape-check | kill-test: stop flag halts everything; sleep-test documented; markouts table accumulates rows within one watch session |
| 8. Lanes (each gated) | verify, hot, political, non-sports manual; then per pre-registered "real if": negRisk exhaustive re-scan, BTC (with settle path), AI matcher (after ground truth), maker shadow, parlays | each lane's own acceptance in its prompt; supervisor owns every loop |
| 9. README | setup, free Odds API key, reading the daily report | written last, from the working system |

**Working method per fix/feature (binding):** reproduce first (quote the file:line or query output) → write the failing test → minimal implementation → full `pytest -q` → `arb report --daily` unchanged except where the change explicitly says otherwise → commit with evidence.

## Operator quickstart — build and run (v3.5)

**Prerequisites:** Python 3.11+, git, an always-on macOS/Linux machine, a free Odds API key (the-odds-api.com; 500 credits/month), optionally an `ANTHROPIC_API_KEY` (AI matcher only, off by default).

**Build** (an executor implements the §16 phases strictly in order, one session per phase, committing per phase with acceptance output pasted into the commit message):

```bash
mkdir ArbitrageModel && cd ArbitrageModel && git init
python3.11 -m venv .venv && source .venv/bin/activate
# Phase 1 creates requirements.txt with EXACT pins; then:
pip install -r requirements.txt
cp .env.example .env                # add ODDS_API_KEY=...
pytest -q                           # must be green — rule 13: nothing runs otherwise
python -m arb report                # Phase 1 acceptance: empty frame, schema created
```

Do not start a phase until the previous phase's acceptance command passes. Phase 3 must record one live fixture per venue per market type and verify every `[VERIFY]`-tagged fact (Kalshi `mutually_exclusive` field, CLOB batch books, volume fields, withdrawal/gas fees, NegRiskAdapter conversion, SX settled endpoint) before anything depending on them is trusted.

**Run** (the supervisor is the only sanctioned way to run loops):

```bash
caffeinate -i python -m arb supervisor      # macOS; or a systemd unit with KeepAlive
python -m arb status                        # heartbeats, newest-quote age, credits, open bets
cat reports/daily/$(date +%F).md            # the product — written 23:59 America/New_York
```

**Stop / restart:** `touch data/stop-all` halts every loop between cycles (a cycle in flight completes its atomic write). Restart only via the documented procedure: commit, `pytest -q` green, remove the flag, relaunch the supervisor. A fix that is not committed does not exist.

**What BUY and SELL mean here:** booking a bet **is** the simulated buy — filled at ask + taker fee at flag time, recorded as `pnl_optimistic`, validated by the confirmation sighting, markouts, and tape check. The simulated sell is the §10.8 exit engine: three pre-registered policies (hold to resolution / take-profit at 80% of flagged edge / trailing stop off peak) computed in parallel from stored quotes, sells priced at bid minus taker fee. The system **never places live orders** (rule 1). The path to live trading is explicit and gated: every deployment gate in §13.4 and the addendum (fee-verified net edge, exhaustiveness proofs, annualized hurdle over a dated risk-free rate, Monte Carlo ruin report, leg-failure rate from the paper simulator, and — for any exit policy — its §10.8 promotion evidence) must pass first, and then a live executor is built as a separate, consciously reviewed component. Nothing in this spec flips live by config.

# Shortcomings ledger — every known defect and its fix

Consolidated register. "Baked in" = the fix is a requirement elsewhere in this spec (section cited).

| # | Shortcoming (evidence) | Fix (where baked in) |
|---|---|---|
| A1 | Settle ignored REVIEW; 314/314 settled arbs were forbidden rows | One eligibility predicate + review settle lane (§11.1, §12) |
| A2 | Reconcile never ran; Kalshi-only resolver = structural no-op | Per-venue resolvers, auto-run, `provisional` labels (§11.2) |
| A3 | 85/314 arbs placed after the event still settled | Timing guards at detection AND settle (§8.4, §11.1) |
| B1 | Zero ladders → capacity couldn't model slippage; always "depth-limited" | `INSUFFICIENT_DEPTH_DATA` until ladder fraction ≥ 0.5 (§13.4) |
| B2 | "$72k profit" = no slippage x 4.5x dup x REVIEW included | position_key dedup + trusted filter + ladder-fraction display (§7.1, §13.4) |
| B3 | depth_usd on three different bases, trusted 100% fillable | normalized taker-notional definition + per-venue unit tests + `fillable_fraction` (§6, §13.4) |
| B4 | Polymarket legs permanently single-level | full CLOB `/book` ladders (§6.2) |
| C0 | NegRisk used Kalshi's fee curve on Polymarket legs → fake baskets | single `leg_fee_cost` + break-even regression vector (§6.5, §7.2) |
| C1 | 60s leg skew counted as "simultaneous" | 10s actionable bound + velocity-decay haircut (§8.4) |
| C2 | No position key; one opinion = many bets | `position_key` on every opportunity/bet + dedup-by-position (§7.1, §10.1) |
| C3 | No portfolio constraint bound detection | exposure runs inside the scan (§13.4) |
| C4 | Capital charged to event start, not settlement | later-venue settlement horizon (§13.4) |
| C5 | No started/final guard at detection | in-play/final/start-unknown guards (§8.4) |
| D1 | Kalshi start fell back to close_time (days late) | never fall back; start-unknown = non-actionable (§6.1) |
| D2 | Doubleheaders collapsed to one event_key → fake arbs | start-time clustering inside market groups (§8.2) |
| D3 | SX priced at zero friction | honest SX pricing; parlay-only 5% modeled in its lane (§6.3, §6.5) |
| D4 | Depth-unknown arbs counted "feasible" | feasible requires real depth basis; unknown routes to micro or non-actionable (§8.4) |
| E1 | Cent-rounding could break the lock, row still recorded | negative-floor arbs flagged and excluded from locked-P&L cells (§7.2 stake math + eligibility) |
| E2 | Rejections were silent | diagnostic rows for every rejection (§8.4) |
| E3 | BTC bets had no settler → permanent OPENs | no class without a registered settlement path (§12) |
| I1 | Poll interleaving created 315 duplicate bets/day | poll-kind ownership of bet lifecycles (§10.2) |
| I2 | Settled-rebook loop re-booked terminal fingerprints | terminal-with-grace + tripwire + blacklist (§10.4) |
| I3 | Fixes sat uncommitted while old process bet (twice) | supervisor + head_sha heartbeats + rule 13 (§13.2) |
| I4 | Host sleep = 3 zero-data days, invisible | caffeinate/launchd + stale-data alarm (§13.3) |
| I5 | `UPDATE flat_bets` survived in a legacy backfill; could NULL-clobber quarantine flags | delete-on-sight + CI grep-test; rule 9 has no labels exemption (§4.9, §10.4) |
| I6 | status.sh keyed on dead process names / child pids | status models supervisor semantics (§13.3) |
| I7 | Three surfaces computed P&L independently and diverged | one `metrics.py` (§14.1) |
| I8 | Confirmed-fill survivorship (+$40 "confirmed" on a −$19 book) | NULL-confirmed counts as missed; markouts are the real measure (§10.3, §10.5) |
| I9 | Config defaults duplicated in three places, drifting silently | typed loader is the only default site (§16 Phase 1) |
| I10 | Web log tracebacks on client disconnect polluted forensics | swallow disconnects, one-line log (§14.3) |
| I11 | Divergence rows: ~1,600 no-ops + 177 zero-stake ledger rows/day | divergences never create ledger rows (§10.1) |
| I12 | AI-matcher recall unmeasurable (zero ground truth ever) | ground-truth set is a prerequisite deliverable (§8.5, §18.5) |
| I13 | Fee facts undated; drifted from published schedules | `source_url` + `confirmed_on` + staleness badge (§6.5) |
| I14 | Hot-watcher 120s cap right-censored every arb lifetime | 900s cap + `censored` tag (§15 `hot`) |
| I15 | Tournament/consensus keys mismatched venue prefixes | `odds_api_sport_aliases` (§6.4) |

# Pre-registration (freeze before any data)

Copy this section into `PREREGISTRATION.md` before the first live poll. Changing any rule after data collection starts is a dated amendment, never an edit. Nulls are publishable results — and every null is **cadence-conditional** (v3.4): claims read "no edge visible at ≥60s observation latency," never "no edge"; sub-minute dislocations are invisible to this instrument by construction.

## Populations

The two XSPREAD venue-pairs (kalshi+polymarket, polymarket+sx) are different populations, pre-registered separately, never sharing a win rate, gate, or headline.

## XSPREAD whitelisting gate (the only path to headline cross-venue P&L)

A venue-pair + market type enters `trusted_pairs` only after: ≥ 99% consistent settlement over ≥ 100 reconciled, position-deduped, clean-lane positions for that pair, **and** a human has read both venues' rulebooks for that market type, **and** the settlement matrix section is fully GREEN. By construction this is weeks away from any fresh start; nothing shortcuts it. A reconciled mismatch reverts the pair immediately. **Note (v3.5):** clearing this gate does not make XSPREADs bookable — 100 mismatch-free positions bound `p_mismatch` only to ≤ 3% (rule of three), and §12 prices that against the edge; positive net requires n ≥ 600. The gate is necessary, the pricing is binding.

## EV lane (frozen OFF, with a re-open trigger)

Inherited verdict: consensus is a worse forecaster than venue mids (Brier 0.238 vs 0.221/0.223; consensus ECE 0.30) and the EV-subset venue side won 420/877 = 0.479. Therefore `ev_enabled: false`. **Re-open trigger:** a re-run of `arb regrade` on **post-cutover data only** (v3.4 — pre-cutover archives carry V1's timing corruption and feed exploratory analysis, never the trigger) where consensus beats venue mids on Brier — as a **paired** comparison (per-event Brier differences, bootstrap 95% CI excluding 0; a point Brier gap of 0.01–0.02 on a few hundred events is routine noise) — AND EV-subset win rate > 0.5 with a 95% CI excluding 0.5 computed by **cluster bootstrap over event_key** (v3.4 — a Wilson interval assumes iid Bernoulli; "clustered Wilson" is not a procedure, and ignoring the clustering makes the CI ~sqrt(2) too narrow at the doc's own n_eff ≈ n/2. The bootstrap is already mandated for the Brier comparison, so one method serves both; alternative considered: Wilson on n_eff = n/DEFF — acceptable, less uniform), with every CI in this trigger clustered by event_key. The regrade computes the Brier comparison **both as-traded** (stale consensus vs current venue mid — the trading question) **and at same timestamps** (scan-time consensus vs scan-time venue mid — the science question): the archived verdict compared an up-to-8-hour-stale consensus against 60-second-fresh mids, so recency alone could explain the whole gap; if consensus wins same-timestamp, the fix is scan timing near events, not abandoning EV. The regrade also evaluates two consensus definitions separately: the soft-book median (falsified once already) and the single sharp anchor (Pinnacle, `consensus_anchor`) — the anchor is the live hypothesis; re-testing only the soft median repeats a failed experiment. If re-opened: go-live for a segment additionally requires mean CLV > 0 (one-sided 95% CI excluding 0, CLV measured against two closing benchmarks: **primary — the venue's own closing mid** (free, already in the quotes table, and the project's best-measured forecaster); secondary — the no-vig consensus close, the last stored consensus within `clv_window_mins` (30) of event start (on re-open the scheduler adds a per-event T−30m consensus fetch, credit-budgeted; bets with no in-window consensus close are excluded from that benchmark's sample but counted in coverage); fees excluded from the metric) over ≥ 190 settled MLB EV bets, AND segment **debiased** ECE ≤ `scoring.ece_max` (0.05; see the config's estimator note — raw ECE ≤ 0.10 was mostly estimator noise at n=200) with positive Brier skill. Kill rule: 10 consecutive losing forward-test bets halts the test (pre-registered false-kill note: under a null 50% loss rate, a 10-loss run appears somewhere in 190 bets ≈ 20% of the time — a kill is halt-and-review, not itself evidence of negative edge). ROI alone never promotes anything.

## Capacity / weighted-ARB enablement

`arb_size_by_ladder` flips true only after (1) ≥ 50% of feasible arbs carry real multi-level ladders and (2) the capacity report shows genuine edge-limited (not depth-fantasy) arbs. Until both: capacity says UNKNOWN, no weighted headline exists.

## Class-specific "real if" clauses

- **NEGRISK:** real if corrected-fee basket edge > `negrisk.min_edge` appears at all in the exhaustive scan; claim requires 30 settled baskets — only §8.4 proof-passing baskets count toward the 30; `unproven-exhaustive` REVIEW baskets are tracked but can never promote the class. (Prior: all archived baskets 1.005–1.011; corrected fees shift ≤ ~0.01 — fees were not the binding constraint.)
- **BTC windows:** real only if the two oracles print the same binary result over ≥ N pre-set windows before `trusted` is even discussed; whitelisting still needs the §18.2 gate.
- **AI matcher:** no Haiku budget expansion until a hand-labeled ground-truth set exists and stage-1 recall ≥ 80%; 50–80% = tune first; segregated paper lane must show settlement consistency at the arb-settle bar before any human-map promotion shortcut.
- **Maker shadow lane:** output is only "share of flags that flip +EV as maker" — an upper bound, never promoted on the flip fraction alone.
- **Ladder-aware totals:** first deliverable is verifying strike-level pairing fires at all (0 ARB may be the fees/edge null, not a matching gap).
- **Minimum samples:** 100 reconciled positions per XSPREAD pair; 190 settled EV bets; 200 settled quotes per calibration segment; 30 settled negRisk baskets; segment scoring blocked below n=30. Halfway checkpoints monitor, never promote. All CIs cluster standard errors by `event_key` — a winner bet and a totals bet on one game are not independent, and ladder strikes are highly correlated (190 bets may be ~90 effective events). Known residual (v3.4): same-day same-league totals share weather/park drivers *across* events — event clustering does not capture it; treat day-level correlation as an acknowledged limitation of these SEs. Family-wise control across the many lanes: any positive result must **replicate in a second out-of-sample month** before promotion — replication, not tighter alphas, is the honest control.

**Power disclosure (v3.4):** none of these n's was power-derived. At n_eff ≈ 90 effective events (190 bets, per the clustering note above), the win-rate gate's minimum detectable effect at 80% power / 95% confidence is `(1.96 + 0.84) × sqrt(0.25/90) ≈ 0.148` — the trigger reliably detects only true win rates ≳ 0.65. The gates are therefore promotion-safe but **cannot detect a modest (0.55) real edge**; a non-firing trigger is the *expected* outcome even under a modest edge and must never be cited as evidence of no edge. Detecting 0.55 would need ~600 effective events. [NEEDS INPUT: choose — accept the 0.65 detection bar as the design, or extend the collection horizon to ~600 effective events per segment; this is a pre-registration decision that must be made before the first live poll.]

# Known traps (print this list above the desk)

1. ROI > ~4% means a stale quote, suspended market, or settlement mismatch — flag, don't celebrate (`arb_stale_warn`).
2. Gamma `outcomePrices` are not executable; CLOB only.
3. Kalshi ask 1.0000 / bid 0.0000 / liquidity 0 = empty book.
4. SX `percentageOdds` is the maker's side — forgetting `1 - p` inverts every price.
5. Totals: 8.5 ≠ 9.0; exact line only; but pair *every* Kalshi strike.
6. Break-window (`live=true`) quotes go stale in seconds; never blend into headline numbers.
7. The optimistic−confirmed gap is only a persistence proxy; markouts are the adverse-selection measure; if confirmed ≈ 0 while optimistic looks great, the strategy doesn't survive reality.
8. Alias per sport; non-sports never auto-matched into the trusted path.
9. A fix that isn't committed doesn't exist; a loop not under the supervisor isn't running.
10. Every "huge edge" is an input bug until proven otherwise — the largest V1 raw edge was 54,500%, and it was an empty book.
11. TeamA-YES + TeamB-YES does not cover a draw — an "arb" without a covering-set proof is a short position in the missing outcome.
12. A book with prices but zero volume is a zombie, not a market.
13. A fresher forecaster beats a staler one on Brier by recency alone — compare forecasters at the same timestamp or the comparison is about staleness, not information.

# Practicality: costs, effort, expectations

**Money.** $0 venue-side (all keyless public reads). The Odds API free tier (500 credits/mo) suffices with activity gating: MLB-only steady state ≈ 8–24 credits/day including settles; tournament windows front-load more. Haiku matcher: cents/day under the verdict cache and budget cap. Hardware: any always-on machine; the DB grows ~4–8 GB/month at full quote persistence (617k quotes/day ≈ 18.5M rows/month × 200–400 B/row incl. ladder_json; the earlier 0.5–1 and 1–3 GB figures were both low) — keep everything, rotating monthly into archive files if size bites; storage is the cheapest input to honesty.

**Build effort.** Phases 1–6 are the core instrument (~6–9k lines with tests, based on V1/V2 actuals of ~9k lines in `arb/` plus ~100 test files); an Opus-class executor lands them in ordered sessions, one phase per session, committing per phase. Phase 7 ops and Phase 8 lanes are incremental.

**What to expect from the data.** Weeks one–two: plumbing checks, zero clean settled evidence (that's correct — the measurement clock starts at the first supervised restart on a green tree). The first real deliverables are the daily report's honesty metrics (flagged vs. realized edge, cadence, left-on-table) and the negRisk/ladder-totals verdicts at their pre-registered n. If everything nulls out, the system has done its job at the cost of electricity — and the maker-side, markout-near-zero, or venue-leads-books findings would each still be independently valuable.

**The one rule that outranks the rest.** This instrument's value is that its numbers can be believed. Any time saved by skipping a gate, blending a lane, or patching a row in place is repaid with the entire evidentiary record. Slow is honest, and honest was the point.
