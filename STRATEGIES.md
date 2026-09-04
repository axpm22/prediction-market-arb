# Strategy Catalog v2.2 — audited + creative extensions + hybrids

2026-07-27. v1 (five-agent deep research) was adversarially audited the same day: one red-team pass (73 distinct holes), one live fact-resolution pass (9 open facts), one local connectivity check. Every hole is dispositioned below: **fixed**, **reframed**, **merged**, **demoted**, or **scrapped**. v2.1 (same day) appends §8: the C-series — sixteen creative lanes generated under the same audit discipline, three load-bearing feasibility facts pinged live. v2.2 (2026-07-28) appends §9: the H-series — hybrid lanes that compose the validated building blocks into a two-book portfolio (small edges often / big edges rarely), with the frequency spectrum and sequencing dependencies made explicit. Logic only — no code.

Evidence labels: **[measured]** / **[claimed]** / **[speculative]**.

---

## 0. The executability reality (read first)

**Nothing in our venue universe is executable from France today.** Kalshi does not serve French residents (France is on the restricted-jurisdictions list since its Oct 2025 international launch — [help.kalshi.com](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states)) [measured]. Polymarket disabled French trading in Nov 2024 and is ANJ ISP-blocked since 2026-07-16 [measured]. Betting exchanges are illegal under the 2010 French law [measured]. SX Bet falls under ANJ's Feb 2026 illegal-gambling classification of prediction markets [claimed]. Only ANJ-licensed sportsbooks are legally usable, and those are data-only in our stack.

So this catalog is **pure measurement**: every lane's value is knowledge (publishable numbers the field lacks), calibration of our own eligibility machinery (§18.2 gate inputs), and optionality if the regulatory state changes (Polymarket is challenging the block). Any lane whose only value was "we could trade this" has been re-scored accordingly. Local check 2026-07-27: Polymarket gamma/CLOB and Kalshi APIs all reachable from this machine (HTTP 200) — data collection works today, fragility flagged.

---

## 1. Audit verdict table

| # | Strategy (v1) | Disposition | Reason in one line |
|---|---|---|---|
| S1 | YES+NO sum | **MERGED into S2** | strict special case; two lanes would double-report one phenomenon |
| S2 | Coherence monitor (baskets + S1) | **KEEP, reframed** | polling can't see sub-minute arbs — metric redefined as censored snapshot prevalence |
| S3 | Combinatorial/ladders | **SCRAPPED as strategy** | $95K/yr global ceiling; salvage = implication-pair settlement-agreement audit (diagnostic) |
| S4 | PM↔PM | **SPLIT: capture NOW, analysis later** | the data is perishable (ANJ block); pooled base-rate primary, per-cell empirics can never power |
| S5 | PM↔book core lane | **KEEP, redefined** | its EV question was S8 in disguise; unique output = settlement-mismatch rate + staleness forensics |
| S6 | Book↔book | **REFRAMED: overround telemetry** | arb "lifetime" at cron cadence is pure censoring; `fr` region (5 ANJ books) confirmed to exist |
| S7 | Exchanges / SX | **DOA / demoted to passive** | exchanges illegal; SX chain deprecation "coming months" — sunset tripwire, excluded from pooled stats |
| S8 | Value vs de-vigged close | **KEEP (Now), redesigned** | CLV needs close-capture we don't do — settlement-graded ROI is primary; de-vig graded vs outcomes |
| S9 | Fed vs FedWatch | **SCRAPPED** | no free official feed (OAuth-entitled, EOD-only); ~4 meetings of sample; headline gap likely definitional |
| S10 | Middles | **SCRAPPED** | alt-line data isn't in any feed we can afford; no free version of this lane exists |
| S11 | Simulated MM | **KEEP (Later), reframed as bounds** | cron-cadence quoter is a paralyzed strawman; deliverable = optimistic-fill upper bound, needs 1-min collector decision |
| S12 | Parlay/combo | **SCRAPPED (revivable)** | gap vs our own correlation model is unfalsifiable; revive only settlement-graded on cross-event combos |
| S13 | Bonding tail-rate | **KEEP, promoted + retrospective** | Kalshi settled-history API confirmed keyless — cohort study now, prospective later; kill-test framing |
| S14 | Dispute-window | **KEEP as passive case-log** | no dispute-detection feed; paths will be sparse points — disclosed, zero effort |
| S15 | Staleness diagnostic | **DEMOTED to Next, redesigned** | headline lag distribution unidentifiable at cron cadence; needs scoped per-game burst protocol |
| S16 | Whale/flow copy | **SCRAPPED (wallet-copy)** | PM-only + fragile + follower-fill fallacy; salvage = large-print markout on Kalshi tape (minor, Later) |
| S17 | Calibration harvests | **KEEP, top priority, restructured** | retrospective Kalshi settled cohort is the primary study; prospective lane becomes out-of-sample confirmation |

Scrapped: **S3, S9, S10, S12, S16** (with salvage/revival conditions noted). Everything else survives only with the fixes below.

---

## 2. Blockers — nothing runs before these

- **B0. Fee-table pin.** The audit caught an arithmetic self-contradiction in v1's own fact sheet: `ceil(0.07·P·(1−P)·100)/100` at P=0.5 gives **2¢, not 1.75¢**, and per-contract vs per-order rounding swings extreme-price fees ~5× — 10–50% of the 1–3¢ edges half the catalog chases. Resolved by fact-check: **Kalshi makers pay 25% of the taker rate** (`0.0175·C·P·(1−P)`, round-up; index series halved) — the "makers free" premise was stale. Still open: **rounding granularity (per-contract vs per-order)** — pin against the fee-schedule PDF (kalshi.com rate-limited the fetch; triangulated from help center + 4 concordant secondaries). Until pinned, all after-fee math carries both bounds. Polymarket resolved: makers never pay; rebates **20% (crypto, sports) / 25% (other fee-charging categories)**; and **fees apply only to markets deployed on or after category activation** — legacy markets are still fee-free, which changes where coherence residue (S2) can persist.
- **B1. Credit budget table.** Five lanes draw on the same 500 Odds-API credits/month (cost = 1 credit × markets × regions per call; historical snapshots are **paid-plan-only at 10 credits/region/market**; scores endpoint cost TBD). v1 called two lanes "cheap" that aren't (S6 adds a region multiplier; S10 needed alt-line markets). Rule: **a lane without a credit line item does not run; allocations sum ≤ 400, keeping the 100-credit warning reserve** (hard rule 2). The table lives in the queue (§4).
- **B2. Statistics rules adopted** (§5 rules 11–13: unit-of-analysis, multiple testing, skew tolerance). Without them the first published curves would be pseudo-replicated forking-paths artifacts — the exact failure the instrument exists to prevent.
- **B3. Polymarket perishable-capture switch-on.** Books, resolution-rule texts, and settlements for mapped pairs start persisting **now** (passive, free, keyless — verified), because the ANJ block can truncate the sample any week. Analysis stays Later; capture does not.
- **B4. Operator decision (cannot be resolved here): non-France collection host.** If PM/Kalshi data access from France dies, is collecting via a non-France host acceptable legally and under venue ToS? This is a legal/ethical call for the operator, surfaced explicitly — the doc does not resolve it silently in either direction. Until decided: capture from here, log outages (rule 15), accept truncation risk.

---

## 3. Surviving lanes — holes found, fixes applied

### L1 (was S2+S1). Intra-venue coherence monitor — Polymarket baskets + Kalshi field-sums
**Claim it can honestly make:** snapshot prevalence of executable-at-our-latency coherence violations, after venue-exact fees, with depth — a **censored lower bound**, not "market efficiency."
**Holes → fixes:**
- Sub-minute closure [measured: hours→<1min recovery; 3.6s in-play half-life] vs cron cadence ⇒ anything we see is the residue. *Fix:* metric = prevalence per snapshot + survival-past-next-poll; "time-to-closure" dropped (pure interval-censoring).
- Sequential leg fetches fabricate basket violations. *Fix:* per-leg book timestamps recorded; pre-registered max cross-leg skew (Polymarket `POST /books` batches; Kalshi lacks batch — events whose legs span > tolerance are invalid observations).
- Violations must be **executable crosses** (buy every leg at ask, after fees, at depth) — mid-based flags banned (rule 1).
- Full-basket buys include the tail leg, which rule 8 forbade — contradiction. *Fix:* rule 8 rewritten: it bans *naked* tail legs; complete baskets are exempt (the tail's miscalibration is priced into the basket sum).
- Polymarket legacy (pre-fee-activation) markets are still fee-free [measured] ⇒ residue may persist there specifically. *Fix:* fee-regime is a covariate, not a constant.
- ANJ-block truncation risk. *Fix:* pre-registered outage rule + Kalshi-field-sum-only fallback.

### L2 (was S4). PM↔PM divergence + settlement-mismatch base rate — capture now, analyze later
**Why it survives:** best-evidenced persistent divergence in the literature (2–4% execution-aware, on the ~6% of events cross-listed [measured, arXiv:2601.01706] — generalization caveat now attached), and the settlement-mismatch base rate is genuinely unpublished. Cardi B (opposite settlements) and the UMA flips are the canonical burns.
**Holes → fixes:**
- Perishable data sat in "Later" — *fixed: capture promoted to Now (B3).*
- Per-venue-pair × market-type mismatch cells can never reach power at rule-11-throttled pair counts (tens). *Fix:* **pooled mismatch base rate is the pre-registered primary**; the §18.2 whitelist gate consumes pooled rate + rule-text diff, not per-cell empirics.
- Cross-venue snapshot skew fabricates lock-ins. *Fix:* both venues' book times stamped; lock-in stats conditioned on skew (rule 13).
- Manual mapping biases the universe toward headline pairs (thinnest spreads, thickest competition). *Fix:* claims restricted to "mapped-pair divergence series," never "the PM↔PM edge."
- P&L is settlement-realized only (hold-to-resolution); no convergence-timing (rotation) modeling — that requires fill and timing assumptions we can't validate.
- Bonus salvage from scrapped S3: for hand-curated implication/date-ladder pairs, audit **settlement agreement on ALL pairs regardless of price violation** — same pipeline, 10–50× the n, and it doubles as a matching-bug detector.

### L3 (was S5). PM↔book cross-domain lane — mismatch + staleness forensics
**Redefinition:** the "is the PM side +EV" question was S8 wearing a second name — it routes there (no double-reporting, rule 17). L3's unique outputs: (a) settlement-rule mismatch rate between book conventions and PM/Kalshi contracts, (b) staleness-conditioned divergence structure.
**Holes → fixes:**
- Book settlement rulebooks exist in no feed. *Fix:* scoped prerequisite — hand-built rules library for the top-N books actually in our feed (OT/extra-time, tennis retirement conventions, void/postponement, 3-way vs 2-way, DNP), versioned in the repo; without it the pair-diff deliverable is vaporware.
- The "books lead on sports" prior was propped on r²≈0.997 close-vs-outcome — a **calibration** statistic, not lead-lag. *Fix:* prior downgraded to [speculative] until L5 measures actual lag; interpretation language de-fanged.
- "Real-arb rate" had tunable free parameters. *Fix:* pre-registered parameter vector (staleness cutoff, stake, fee-table date) frozen with the lane.
- Decision-value statement (a 500-credit budget buys no luxuries): the number calibrates eligibility thresholds and is the publishable headline; it is *not* a harvestable edge for this operator (see §0).

### L4 (was S8). Settled-outcome value measurement vs de-vigged benchmark
**Redesign:** **settlement-graded ROI is the primary**; CLV demoted to secondary, computed only where a close-proxy exists.
**Holes → fixes:**
- CLV needs closing lines we don't capture; at cron cadence "close" = last poll, hours stale, with error correlated to our own cadence — the exact confounder the lane exists to avoid. *Fix:* settlement grading first; optionally a credit-budgeted per-game pre-start scan for a subsample (explicit line item in B1), CLV claims restricted to that subsample with the pseudo-close gap T reported.
- Circularity: the same de-vig stack set thresholds, defined the benchmark, and graded results. *Fix:* de-vig methods (multiplicative / power / Shin) are graded **against settled outcomes** (calibration/Brier by method), never against each other.
- Benchmark composition unpinned. *Fix:* pinned book list from our actual feed (Pinnacle via `eu` region — presence verified in-feed before switch-on); if Pinnacle is absent the benchmark is soft-consensus and the cited ROI literature does not transfer — say so in the prereg.
- venue × band × method grid = forking paths. *Fix:* one pooled pre-registered primary (e.g., after-fee paper ROI of quotes beating the pinned benchmark by ≥ frozen threshold); the grid is exploratory, labeled as such (rule 12).

### L5 (was S15). Pre-match staleness study — redesigned burst protocol
**Holes → fixes:**
- As specified it was unbuildable: lag between feeds can't be identified from hours-apart level snapshots + `last_update`; historical 5-min snapshots are paid-plan-only. *Fix:* scoped **per-game burst protocol** — for a pre-registered sample of games, fast-poll Kalshi/PM books (free, keyless, ≲20 req/s assumption) around scheduled windows, paired with a small credit-budgeted burst of Odds API calls (line item in B1); pre-match only.
- In-play explicitly out of scope (3.6s half-lives are unreachable at any budget we have) — the lane's claims say "pre-match staleness," full stop.
- "% of edge attributable to staleness" had no method. *Fix:* pre-registered decomposition — every L3 detection re-priced at freshness-conditioned quotes; the attribution formula is frozen before data.
- Demoted Now → Next: it needs the burst design and a credit line first. (Its slot in Now went to B3 capture.)

### L6 (was S13). Near-certainty tail-rate — retrospective cohort first
**Redesign:** the prospective version could only ever kill (at true tail 0.5–2%, months of data leave the rule-of-three upper bound as large as the whole edge). Fact-check confirmed the fix is available: **Kalshi settled markets + candlesticks are keyless, including pre-cutoff history via `/historical/*` endpoints** [measured, docs]. So: retrospective cohort of all settled markets that ever traded ≥97¢ with a scheduled, verifiable completion time (sports first), measuring (a) empirical P(≥97¢ → settles 0), (b) certification-lag distribution → per-day-locked yield.
**Holes → fixes:**
- Sports/named-source cohorts measure the tail where it's *smallest* and miss the UMA tail where it kills. *Fix:* deliverable relabeled **"named-source tail rate"**; a separate manual non-sports watchlist arm (rule-11-compatible) logs the UMA-resolution cohort descriptively; the two are never pooled.
- Fee at 97–99¢ depends on the unresolved rounding granularity (B0): per-contract round-up would be 1¢ — a third of a 3¢ gross. Both bounds carried until pinned.
- Kill-test framing pre-registered: rule-of-three bound stated up front; validation (as opposed to non-refutation) requires the retrospective n, which is why retrospective runs first.
- Partition vs L8: L6 owns the determined-ground-truth ≥97¢ subset; L8's high-band cells exclude it (no two tail numbers from one dataset, rule 17).
- Polymarket arm: prices-history is keyless (fidelity 1m) but old-token coverage has known blanks [measured, GH issue #189] — validate coverage before the PM cohort is claimed; capture-at-scan remains the safe design.

### L7 (was S11). Simulated maker bounds — Kalshi-first, honest labels
**Holes → fixes:**
- A quoter that re-quotes at cron cadence cannot implement the fast-cancel mitigation the evidence calls mandatory ⇒ the sim is a paralyzed strawman. *Fix:* deliverable relabeled **upper bound on adverse selection / lower bound on maker viability** — a bracketing pair: conservative fill rule (fill only if trade prints strictly through our price) vs optimistic (fill at touch); truth bracketed, point estimates banned.
- Trade-through fills assume queue-front priority and full size ⇒ optimistic by construction — hence the bracket.
- Reward-subsidy leg needs ~1/min two-sidedness sampling (the venue's own cadence) — unreconstructable from cron polls. *Fix:* subsidy leg declared unmeasurable without a minute-cadence collector; building that collector is an explicit infra decision (it breaks the cron-not-daemon architecture) deferred until the markout bracket says maker viability is even in range.
- Fee model updated per B0: **Kalshi makers pay 0.0175·P·(1−P)** (not zero — v1's premise was wrong); Polymarket makers pay zero and earn 20–25% rebates. The maker-vs-taker asymmetry is venue- and category-specific now, not a slogan.
- Kalshi-first (tape is public and keyless; PM tape via data-api is keyless but ANJ-fragile).

### L8 (was S17). Calibration harvests — retrospective primary, prospective confirmation
**Redesign (the biggest promotion):** replicate Bürgi/Whelan-style calibration on the **full Kalshi settled history** (keyless, confirmed) — favorite-longshot, YES-phrasing, horizon, category cells — then use our prospective persisted quotes as the **out-of-sample confirmation** of whichever cells survive. This solves at once: power (retrospective n ≫ 600-event standard for coarse cells), universe bias (our scan slice is sports-heavy and thin exactly where the literature's political cells live — now measured on the full exchange instead), and the winner's-curse pipeline (exploration burned on retrospective; confirmation only on future data).
**Holes → fixes:**
- **Unit of analysis:** per-quote bucketing pseudo-replicates (one market appears once per poll, near-perfectly dependent). *Fix (binding rule 11):* one unit per market (pre-registered snapshot rule, e.g., last quote at T−δ before close per market), cluster-robust intervals otherwise.
- Cell explosion ⇒ pre-registered coarse grid only (e.g., 5 price bands × 3 categories × 2 horizons), one pooled primary per hypothesis, everything else exploratory (rule 12).
- Long-horizon cells are right-censored for months–years. *Fix:* claims scoped to short-horizon bands first; each band's unlock date stated.
- Tail cells have extreme P&L skew ⇒ kill criteria on **exact-binomial calibration-gap CIs**, never on paper P&L.
- Taker/maker bounding: paper fills are taker-style; the measured Kalshi maker premium is not harvestable as taker, and makers now pay 25% of taker anyway (B0). Every prereg carries: *"this lane bounds the taker implementation only; a negative result does not kill maker variants"* (rule 18).

### L9 (was S6). ANJ overround telemetry
**Reframed:** drop arb-counting entirely (a ~13s-median phenomenon sampled at cron cadence yields censoring noise; snapshot prevalence is the only unbiased estimand polling grants — and even that is near-zero-information here). Keep the robust, France-relevant number: **overround/TRJ structure of ANJ books vs sharp reference, by sport and market**, which directly calibrates how big a divergence must be before anything ANJ-side could ever be +EV.
**Enabled by fact-check:** The Odds API has a dedicated **`fr` region: winamax_fr, betclic_fr, unibet_fr, pmu_fr, netbet_fr** (no ParionsSport, no Bet365 FR) [measured, docs]. **Not free:** adding a region multiplies per-call credit cost — runs at low cadence (e.g., weekly snapshot) under its B1 line item.

### L10 (was S14). Dispute case-log
Passive: log price paths of any market we already persist that enters a dispute. Disclosed honestly: with no dispute-detection feed and cron cadence, "paths" = 2–3 sparse points per case; this is a case-series, not a dataset. Zero effort, PM-dependent, keep.

### L11 (was S16, salvage). Large-print markout on Kalshi tape
Wallet-copy is scrapped (Kalshi trades are anonymous; PM wallet data is ANJ-fragile; and the replay granted the follower the whale's own fill — a fiction). Salvage: markout of large prints measured **from the post-print touch on the follower's side** [fix], Kalshi tape only, Later tier, diagnostic value (does size predict direction here?).

### L13 (new 2026-09-02, from the literature). No-side calibration — the Yes-Bias test
L8 reads `yes_ask` and only `yes_ask`, so every band it reports is the return to a taker who bought YES. Its uniformly negative curve is therefore consistent with two different worlds it cannot separate: an efficient market where we pay the spread, or a real mispricing we were standing on the wrong side of.

Source: the 2026 Kalshi microstructure literature reports the favorite-longshot bias is largely a **"Yes Bias"** — YES systematically overpriced — which makes buying NO the mirror trade. Kalshi candles quote only `yes_ask`/`yes_bid`, so the no-ask derives from the identity already in `data/verified_facts.md`: the ask of one side is `1 - the opposite side's best bid`.

Same horizon, same two-sided gate, same bands, same clustering, same family-wise correction as L8, so the two are comparable market-for-market.

**Read the result carefully.** Buying both sides of one market loses exactly the spread plus both fees, so `yes_edge + no_edge == -(spread + fees)` identically (pinned in `tests/test_no_side_calibration.py`). A positive L13 number is the arithmetic MIRROR of a negative L8 number, never a second observation of it. What is new is only the direction — whether either side of these books was payable at all, which L8's design could not ask.

### L14 (new 2026-09-04, from the operator). Whale consensus — the V1 debunk re-tested

L11 scrapped wallet-copy for three named reasons: Kalshi trades are anonymous; PM wallet data is ANJ-fragile; and the replay granted the follower the whale's own fill — a fiction. This design (`docs/superpowers/specs/2026-09-03-whale-consensus-lane-design.md`) answers each and adds four more corrections. The follower's price is the **post-print touch fetched live** (best ask after the whale's prints, plus taker fee) — never the whale's own fill. Whale skill is the whale's own **realized edge in points** on its own fills (`e = payout − avg_price`, sold shares realized at their sell price, VOID excluded), run through an **exact two-level shrinkage** (root → sport → bucket, normal-normal empirical Bayes) and acted on only through its **lower credible bound** (`score_lb`, z=0.8416) — never a raw win rate or a point estimate. **One wallet is one whale per side**: several BUY windows by one wallet on one (market, side) merge into a single contributor, scored once, one vote. **Entry needs both bases** (amended 2026-09-04, PREREGISTRATION row of that date): the fresh-print tally must fire *and* the whales still holding the market must give the same side at least `fire.holders_ratio` (0.6667) under that variant — measured point-in-time over the first 56 fires, 20 (36%) were already below the 0.50 flip ratio on the holders basis at entry, i.e. round trips booked to be flipped out on the next cycle. Open bets carry a **signal-driven exit** (flip / left / confidence, pregame only, priced at bid minus fee, with the hold counterfactual kept) — amended 2026-09-04 (PREREGISTRATION row of that date) after day one: `flip` recounts over the whales still HOLDING rather than the prints still fresh (a print ageing out is not a change of mind), the collector records both legs of every fill so a whale exiting through a resting order is seen selling, and a second `left` test reads each signal whale's current holding from data-api `positions?user=`. Settlement is **Polymarket-native** (its own Gamma resolution reader; no cross-venue oracle, so ANJ fragility is inherited unmitigated and stated, not fixed). Scoring is **point-in-time** (`as_of` required; only positions resolved before it count). And by `classify()` construction the lane is **REVIEW forever** — a positive result is a report, never a lane change.

**Primary:** V2/flat mean settled ROI per $ (V2 points = `score_lb − min_score_lb` per qualifying whale), `event_key`-clustered (`pm:<event_slug>`) **one-sided** bootstrap bounds — 90th percentile upper, 20th percentile lower — never `regrade`'s two-sided 95% default. V1 (flat, 1 point/whale), V3 (size-weighted), V4 (leaderboard-ranked), **V5 elite** and **V6 dominance** run alongside as secondary, sign/magnitude agreement only; none is promotable under this registration without its own dated pre-registration and replication month. V6 (added 2026-09-04) is the only variant that can see MONEY rather than heads — skill-weighted, recency-decayed, price-relative net dollars over the market's own 24h volume — because 12 of the first 56 fired V1 signals (21%) had the whales' net dollars on the side the head count did not buy. V5 (added 2026-09-04) replaces the absolute qualification bar with a RELATIVE one — a whale votes only if its `score_lb` in that bucket is in the top `fire.elite_pct` (20%) of every wallet we have scored in the root, hygiene-excluded wallets removed — because `min_score_lb` 0.0 sits *below* the prior mean (a wallet with no history scores lb ≈ −0.009) and its meaning drifts with the base rate, which moved from +0.0464 to +0.0428 in six hours on 2026-09-04. Measured before registration: top 20% retains 15 of the 56 fires at ≥2 elites, top 10% and top 5% retain none, and top 40% has a cutoff below the qualification bar, which would make V5 a copy of V1.

**Pinning tests:** `tests/test_whale_score.py` fixes the shrinkage worked table (n=5/15/40/120/400 at lb80/lb90) and the point-in-time frozen-clock identity; `tests/test_whale_signal.py` fixes the gate order (sybil merge happens inside the tally before any gate; then ratio → min-points → uncalibrated → late-print → stale → too-close → depth → one-sided), the one-wallet-one-whale merge, and the three exit reasons (flip / left / confidence); `tests/test_whale_lane.py` fixes the `exit:flip` end-to-end path, the `lifetime_closed`-never-stops-an-exit rule, and the position-key variant/sizing suffix.

**Comparable to L8 and L13:** same taker basis (paper fills at the touch, fee-inclusive, rule 1) and the same `event_key` cluster convention for every CI — a follower taking at the post-print touch pays the same structural cost L8/L13 already measured (about 13x the edge on the one fully-measured cover; posting is adversely selected). L14's claim is only that whale consensus is worth more than that cost.

**Status, plainly:** the instrument (watch lane, `data/arb.sqlite`) has been down since 2026-08-09 — no supervisor process running. One `arb whale --once` acceptance cycle ran live on 2026-09-04 (Task 8, head `76da135`; `polls` row id 94743, the lane's only `kind='whale'` row to date) against that stale universe; it booked nothing (0 WHALE `flat_bets`) — no wallet qualified, so no book was ever fetched. The lane has not yet run under the supervisor.

---

## 4. Ranked queue v2 — with the two columns the audit demanded

Power column = months to reach a decision-grade answer for the **pre-registered primary** (600-effective-events standard where applicable; rule-of-three logic for rare events). Credits column = Odds-API credits/month (Kalshi/PM polling is credit-free). "TBD-fill" = must be filled from current scan config before switch-on; the allocations must sum ≤ 400 (B1).

| Tier | Lane | Credits/mo | Expected power timeline | Primary (pre-registered before switch-on) |
|---|---|---|---|---|
| **Blockers** | B0 fee pin; B1 credit table; B2 stats rules; B3 PM capture-on; B4 host decision | 0 | days | — |
| **Now** | L8 retrospective calibration (Kalshi settled history) | 0 | **immediate** (retrospective) | after-fee calibration gap, coarse pre-registered cells, unit = market |
| **Now** | L6 retrospective tail-rate cohort | 0 | **immediate** (retrospective) | P(≥97¢→0) named-source cohort + per-day-locked yield |
| **Now** | L2 capture arm (books, rule texts, settlements for mapped pairs) | 0 | n/a (capture) | — (analysis is Later; pooled mismatch rate) |
| **Now** | L3 core lane forensics upgrade | TBD-fill (existing scan) | mismatch rate: rule-of-three, ~100–200 settled pairs ⇒ ~6–12 mo for a ≤3% bound | pooled settlement-mismatch rate; frozen parameter vector |
| **Now** | L4 settlement-graded value measurement | TBD-fill (existing scan) + optional close-scan line | ROI: needs ~600 effective events — estimate months from current event throughput before prereg | pooled after-fee ROI vs pinned benchmark |
| **Next** | L5 staleness burst study | small burst budget (design first) | per burst-sample design | pre-match lag distribution; frozen attribution formula |
| **Next** | L1 coherence monitor | 0 | prevalence: immediate; but expect ≈0 signal post-fee (that *is* the finding) | censored violation prevalence, fee-regime covariate |
| **Next** | L9 ANJ overround telemetry (weekly) | ~4 × (markets×2 regions) — price exactly in B1 | immediate (descriptive) | overround by book/sport vs sharp reference |
| **Later** | L7 maker bounds (Kalshi) | 0 | after bracket design; subsidy leg blocked on collector decision | markout bracket [conservative, optimistic] |
| **Later** | L2 analysis arm; L8 prospective confirmation; L11 print markout; L10 case-log | 0 | as samples accrue | as pre-registered above |
| **Scrapped** | S3, S9, S10, S12, S16-wallet | — | — | revival conditions in §1 table |

Order rationale (audit's top finding): the two retrospective lanes moved to the front because they are the only ones that reach decision-grade power immediately and cost zero credits; the perishable PM capture outranks every analysis; and no credit-consuming lane runs until B1 prices it.

---

## 5. Measurement rules v2 (binding; 1–10 amended, 11–18 new from audit)

1. **Executable prices only** — fill at ask/bid you'd actually cross, walk the book at stake, fee-inclusive. Mid-based anything is banned.
2. **Legging modeled, honestly labeled** — multi-leg paper trades record "both filled" and "leg-1-only, hedge at next observation" P&L; the latter is a **cadence-bound worst case** (our δ = poll interval, not market δ); use venue candlesticks for finer δ where available.
3. **Fees venue-exact, side-aware, price-band-aware, dated, regime-aware** — incl. Kalshi maker 25%-of-taker, Polymarket legacy-market fee exemption, rounding granularity per B0; fee-table snapshot in `data/verified_facts.md` with dates.
4. **Staleness-stamp every detection** — source `last_update`, our receipt time, both venues' book times.
5. **Depth, not volume** — ~25% of PM volume is wash-like [measured]; capacity claims from displayed depth only.
6. **Annualize per day-locked — and per credit.** Two scarce-resource denominators: capital-days and Odds-API credits. Lane reports carry edge-per-day-locked and finding-per-credit.
7. **Settlement-rule diff at detection; settlement-agreement audit at resolution** — incl. cancellation/refund conventions (the 50¢-refund class) and the versioned book-rules library (L3 prerequisite).
8. **No naked tail legs** — the cheap tail is the systematically overpriced side [measured]; complete simultaneous baskets are exempt (the tail is priced into the basket sum).
9. **Correctness before speed** — outcome-ordering fixture tests per venue; the documented catastrophic class is sign/identity flips, generalized to include near-duplicate market matching within a venue.
10. **Pre-registered kill criteria** — sample size and the negative result that shuts the lane down, declared before switch-on; skew-heavy cells kill on calibration CIs, not P&L.
11. **Unit of analysis** *(new)* — every rate/curve declares its independent unit (market/event, never quote-snapshot); repeated polls of one market are one unit or get cluster-robust intervals.
12. **One primary per lane** *(new)* — grids are exploratory; any cell-level claim needs correction or shrinkage; exploration and confirmation samples are split (retrospective explores, prospective confirms).
13. **Simultaneity = pre-registered skew tolerance** *(new)* — "same timestamp" is defined in seconds, per lane; observations exceeding cross-leg/cross-venue skew are invalid, not approximated.
14. **Clock discipline** *(new)* — one master clock (our receipt time) for all cross-feed comparisons; server times recorded but never mixed across feeds as if comparable.
15. **Outage logging + coverage mask** *(new)* — every published series carries its collection-coverage mask; venue-access loss (ANJ risk) must be distinguishable from market change.
16. **Error-quote filter, pre-registered** *(new)* — palpable-error/outlier policy frozen before data; headline numbers reported with and without the filter.
17. **No double-reporting** *(new)* — one signal, one lane: L4 owns EV-vs-benchmark, L3 owns mismatch/staleness, L6 owns the determined-truth ≥97¢ subset (excluded from L8 cells), L1 subsumes S1.
18. **Implementation bounding** *(new)* — taker-style paper results bound taker implementations only; kill criteria never generalize to maker variants (and vice versa).
19. **Regime dating** *(new)* — every evidence prior and every result is tagged with its fee/regulatory regime (pre/post 2026-03-30 PM fees; pre/post 2026-07-16 ANJ block); cross-regime pooling is forbidden. Sample windows state their sport-mix seasonality.

---

## 6. Fake-arb taxonomy v2 (ranked; classes 8–11 added by audit)

1. **Stale quote / feed lag** — dominant [measured].
2. **Fees & rails** — now includes regime boundaries (legacy-market exemptions) [measured].
3. **Liquidity ghost** — displayed depth unfillable [measured].
4. **Settlement-rule mismatch** — highest severity [measured: Cardi B, tennis retirement, cancellation refunds].
5. **Limits/voids on the book leg** [claimed/insider].
6. **Resolution-source / oracle risk** [measured: UMA flips].
7. **Identity mismatch** — sign/ordering bugs *and* near-duplicate market mismatches [measured, receipts].
8. **Suspended-but-quoted markets** *(new)* — a present-but-untradeable price is not staleness; needs its own detection heuristic (e.g., book flagged suspended, PM paused flag).
9. **FX/stablecoin exposure on locked capital** *(new)* — EUR operator, USDC legs, 90-day locks: FX variance rivals the 2–4% edges being annualized; per-day-locked reporting gets an FX-adjusted variant.
10. **Venue death / custody / forced exit** *(new)* — SX chain deprecation "coming months" [measured]; open positions under a hardened geo-block; sunset tripwires (depth/error thresholds) freeze a venue and exclude it from pooled stats.
11. **Correlated-leg clustering** *(new)* — many same-night detections share one game/rule/oracle; portfolio risk ≠ per-trade risk; report detection clustering by game and rule-type.

---

## 7. Fact sheet v2 (resolved 2026-07-27; open pins flagged)

| Fact | Value | Status |
|---|---|---|
| Kalshi taker fee | `0.07·C·P·(1−P)`, rounded up; **round-up granularity (per-contract vs per-order) OPEN — B0**; at P=.5 per-contract rounding ⇒ 2¢, not v1's stated 1.75¢ | resolved formula / open rounding |
| Kalshi maker fee | **25% of taker: `0.0175·C·P·(1−P)`**, charged on execution of resting orders; index series (INX*/NASDAQ100*) at half multiplier; "some markets differ" | resolved via help center + 4 concordant secondaries; PDF fetch 429'd — verify verbatim when possible |
| Polymarket fees | takers only, `C·feeRate·p·(1−p)`: crypto 0.07, sports 0.05, econ/culture/weather/other 0.05, finance/politics/mentions/tech 0.04, **geopolitics 0**; **only markets deployed after category activation** (legacy fee-free); maker rebates **20% crypto+sports / 25% others**, daily USDC | resolved ([trading-fees](https://help.polymarket.com/en/articles/13364478-trading-fees), [rebates](https://help.polymarket.com/en/articles/13364471-maker-rebates-program)) |
| Kalshi from France | **not served** — France restricted since Oct 2025 international launch; no ANJ action against Kalshi as of 2026-07-27 | resolved; monitor weekly (tripwire) |
| Polymarket from France | trading disabled Nov 2024; ANJ ISP block 2026-07-16, legal challenge filed ~07-23; **APIs reachable from this machine today (HTTP 200)** | resolved; fragile |
| Kalshi public API | market data unauth (`security: []`): markets, order books, trades, candlesticks (1m/1h/1d), settled filter, plus `/historical/*` for pre-cutoff (~3-mo live window); rate limit unpublished for unauth — assume ≤20 req/s, back off on 429 | resolved (docs.kalshi.com) |
| Polymarket history | keyless: gamma resolved markets; CLOB `prices-history` (fidelity 1m); **old-token coverage has known blanks** — validate per cohort | resolved with caveat (GH issue #189) |
| The Odds API | regions us/us2/uk/eu/**fr**/se/au; fr books: winamax_fr, betclic_fr, unibet_fr, pmu_fr, netbet_fr (no ParionsSport/Bet365-FR); cost = 1 credit × markets × regions; **historical endpoints paid-plan-only, 10 credits/region/market**; scores-endpoint cost TBD; Pinnacle presence in our pulled regions must be verified in-feed (L4 prerequisite) | resolved (docs) / two TBDs |
| Kalshi combos | same-game AND cross-game legs, own order books, product settlement; football/basketball/Mentions only; Combo Incentive = opt-in maker subsidy | resolved — S12 stays scrapped (correlated product settlement) |
| CME FedWatch | official API is OAuth-entitled, EOD-only, per-meeting 25bp-tree; **no free official feed** | resolved — S9 scrapped; revive only with a legitimate free per-meeting source |
| SX Bet | live (api metadata OK, chain SXR) but token sunset done, ws API deprecated 2026-07-01, **chain "planned to be deprecated in the coming months"** | resolved — passive + tripwire |
| ANJ TRJ cap | 85% annual player-return cap on sports betting | unchanged [measured, regulatory] |
| PM volume contamination | ~25% wash-like (weekly peaks ~60%) — depth, not volume | unchanged [measured] |

Evidence-hygiene corrections carried from the audit: the 19.2% book-vs-exchange arb frequency stays [measured] but its transfer to PM-vs-book is now labeled **[speculative analogy]**; the 2–4% divergence carries its ~6%-overlap generalization caveat; practitioner constants (38% legging drag, $200K latency profits) are survivorship-biased anecdotes bounding risk classes, never rate estimates in preregs.

---

## 8. C-series — creative extensions (v2.1)

Generation rules: every lane below is (a) paper-measurable with data we already have or can get free and keyless, (b) self-audited — its own holes are stated, not discovered later, (c) subject to rules 1–19 and the B-blockers; none preempts the Now tier. The organizing insight: **stop fighting on lanes where the competition's advantage is speed, and fight where our structural position is different** — we own a growing multi-venue quote archive, an LLM budget, a settle pipeline, and (uniquely) a front-row seat to a regulatory natural experiment. Feasibility pings run 2026-07-27: Kalshi `KXHIGHNY` daily-high-temp series is live (category "Climate and Weather", named Source Agencies, insider-trading prohibition in the terms — the venue itself treats the settlement feed as the edge); NWS `api.weather.gov` observations return free JSON; BLS public API returns CPI JSON through June 2026. All [measured, this machine].

### Theme I — Own the settlement source

**C1. Settlement-source-native lanes (weather first).** Every latency strategy we scrapped loses because someone else's truth feed is faster. Invert it: trade **only markets whose named settlement source is a free, official, machine-readable feed we poll ourselves** — then we learn truth at the same speed as anyone on earth, and oracle risk ≈ 0 by construction (the settlement input *is* our data). The map: Kalshi weather → NWS observations/climate reports; CPI/jobs ladders → BLS API; GDP → BEA; oil inventories → EIA; sports scores → already in our stack. Weather is the flagship: daily-high markets become near-certainties **hours before close** (once the day's peak heat passes, the remaining-day exceedance probability collapses) — a bonding trade (L6) where we compute the certainty ourselves instead of trusting the crowd. Domer's rule ("go where experts are scarce") applies: weather markets are hobbyist territory, not bot territory. **Holes, stated now:** (i) settlement uses the official *climate report*, which can differ from preliminary METAR observations (rounding, corrections) — the prelim-vs-final revision rate is itself sub-deliverable #1, and until measured it bounds the "zero oracle risk" claim; (ii) competition is unverified — if spreads already snap shut at each hourly observation, the edge is occupied (the spread-response-to-obs lag is sub-deliverable #2); (iii) hourly METAR cadence, not real-time — fine for cron. **Paper test:** join NWS obs series to persisted KXHIGH books in event time; measure price-vs-physically-locked-outcome gaps, when they close, and simulated after-fee taker/maker P&L on "physically decided, not yet priced" signals.
**C2. Scheduled-event burst microstructure ("cron-compatible HFT").** A cron system can't stream — but it can **wake up on a calendar**. Every econ print (CPI 08:30 ET), FOMC statement, weather-market close, and game start is a pre-known timestamp: burst-poll Kalshi/PM books (free, keyless) at T−60m…T+15m around them. Deliverables: pre-release drift (does the book leak?), incorporation speed, overshoot/reversion in event time — pooled across dozens of releases. This generalizes L5's burst design from a diagnostic into a strategy family: post-release reversion/drift harvesting where the burst shows it exists. **Holes:** burst windows are pre-registered or they're forking paths (rule 12); Kalshi rate limits unpublished for unauth (assume ≤20 req/s, back off); only *pre-match/pre-release* claims — anything sub-minute stays out of reach honestly.
**C3. Implied-vs-realized distribution on econ ladders (prediction-market "vol surface").** Kalshi's bucketed CPI/NFP/rates ladders are a discrete implied distribution over the print. Compare, over many releases, the **implied distribution at T−1d vs the realized print distribution**: are the wings (tail buckets) systematically rich — the FLB expressed as overpriced tails of a *numeric* distribution? This is the PM analogue of implied-vs-realized volatility, runnable **retrospectively today** on Kalshi settled history + candles (keyless, confirmed). **Holes:** per-release the outcome is one draw — the unit is the release (rule 11), power comes from pooling dozens of releases per series and grading with proper scoring rules (CRPS-style), not per-bucket P&L; bucket boundaries change across months (regime-tag them).

### Theme II — Price the meta-game

**C4. The divergence accounting identity (capstone framework).** Stop treating cross-venue divergence as one number. Decompose it: **observed divergence ≡ staleness + fees + wall premium + oracle premium + semantic risk + ε** — and notice each term is already owned by a lane: staleness (L5), fees (B0 tables), semantic risk (C5/rule-11 calibration), oracle premium (this lane), wall premium (the residual that persists when everything else is controlled). Oracle premium gets identified by **matched triplets**: the same event listed under different resolution mechanisms (Kalshi named-source vs Polymarket UMA vs book grading) — the systematic price haircut on the subjective-oracle version *is the market price of oracle risk*, a number nobody has published. **Holes:** identification is fragile (triplets are rare; ~6% cross-listing shrinks further); collinearity between walls and oracle terms across venue-pairs — report the decomposition with explicit confounding caveats, as accounting, not causal inference.
**C5. Ambiguity premium via rule-text scoring.** We already pay for an LLM budget (rule 11). Point it at a new target: score every market's resolution text for ambiguity (subjective terms, undefined edge cases, source hierarchy depth) on a frozen rubric, then test whether the score predicts **dispute probability, spread width, calibration error, and settlement-mismatch incidence**. If it does, the score becomes an automated eligibility input (auto-quarantine high-ambiguity markets) — the instrument grades market *language*, not just prices. **Holes:** rubric drift (freeze the prompt + eval set exactly like the matcher's 60-case suite); circularity if the score is trained on the same disputes it predicts (temporal split: score first, disputes accrue later); budget-capped per rule 11.
**C15. Settlement-graded combo calibration (S12 revived falsifiably).** v2 scrapped combo-vs-model comparisons because the correlation model was ours. The falsifiable version needs no model: collect Kalshi combo prices (each combo has its own order book — confirmed) and grade them **against realized joint outcomes** over many combos. Books' parlays are notoriously margin-stacked; whether *exchange-traded* combos carry the same overpricing is an open, publishable number. **Holes:** power — combos multiply, but pooling across heterogeneous leg-counts needs a pre-registered normalization (grade by implied-vs-realized log-loss per leg-count stratum); football/basketball/Mentions only; same-game product-settlement quirks (rule-text check per combo class).

### Theme III — Structure and curves

**C6. Implied hazard curves from date ladders.** S3 died as violation-arb; the curve it leaves behind is alive. A family "X by March / by June / by Sept" is a monotone CDF in disguise — differencing it yields the market's **implied hazard rate of X per period**. Deliverables: hazard-curve atlas by category; kink detection (a hazard spike with no scheduled catalyst is a mispricing candidate *or* a market microstructure artifact — both worth knowing); comparison against base-rate priors where objective ones exist (e.g., historical frequency of rate moves per meeting). Relative value, not arb — no basket execution needed, so the capacity objection that killed S3 doesn't apply to the *measurement*. **Holes:** ladder legs differ subtly in wording more often than they violate monotonicity (C5's scorer pre-screens pairs); thin far-dated legs = ghost quotes (depth rule 5).
**C7. Informal-partition coherence ("bots police only machine-readable structure").** Formal NegRisk groups are arbed in milliseconds [measured]. But logically exhaustive partitions listed as *separate binary markets* — or split across venues — have no mint/merge primitive and no bot coverage. Hypothesis, falsifiable: **partition-sum deviations are larger and longer-lived on informal partitions than formal groups.** A hand-curated partition registry (rule-11-compatible) + persisted books measures both populations with the same method. If confirmed, it localizes exactly where structural edge survives the bots. **Holes:** hand-curation selects legible partitions (state the selection rule); "exhaustive" is a resolution-text claim — every registry entry needs the C5 ambiguity screen; capacity expectations stay toy-sized (this is a where-does-edge-hide measurement, not an income plan).
**C8. Phrasing-pair natural experiments.** The YES-bias literature says affirmative phrasing is overpriced — but existing tests compare *different* events. Clean design: find **mirrored pairs of the same proposition** (one venue's "Will X happen?" vs another's negation-framed contract, or intra-venue complements). In a mirrored pair, event risk cancels exactly; the residual price gap is *pure framing*. This is the rare behavioral test with a built-in hedge — measurable as a paired-difference series with tight power per pair. **Holes:** true mirrors are rare (expect tens, not hundreds — report n before claiming anything); mirrors that aren't (subtle scope differences) are the C5 screen's job; fee asymmetry between venues must be netted before calling the residual "bias."

### Theme IV — The certainty complex (extends L6)

**C9. The certainty discount as a market-implied venue rate — plus its harvest.** A 99¢-certain market is functionally a **zero-coupon bond issued by the venue's settlement process**; its discount is an interest rate that embeds custody risk, certification lag, and (on Polymarket) USDC risk. Three deliverables from one dataset: (i) the **venue repo-rate series** — track the certainty discount over time per venue; a widening discount with no event-risk explanation is a *venue-stress indicator*, our version of a repo spread blowing out (feeds taxonomy class 10 tripwires); (ii) **calendar effects** — certification happens on human schedules; if Friday entries lock capital through weekends, the discount should show a measurable day-of-week term structure ("Friday certainty discount"); (iii) **impatience-liquidity provision** — in decided markets, stragglers dump at 95–97¢ into thin books; a resting bid there earns the impatience premium with *minimal adverse selection because the event is already decided* — the one maker trade where the usual toxicity argument inverts. All retrospectively measurable from settled history + tape. **Holes:** (iii)'s "decided" flag must come from our own ground-truth feed (C1/scores), never from the price itself (circularity); dispute tail still applies — L6's tail-rate bounds it; USDC-vs-EUR component separated using public USDC series (taxonomy class 9) before calling anything "venue" risk.

### Theme V — Flow forensics

**C10. Subsidy shadow map.** Liquidity rewards create *fake depth*: reward-optimal quoters cluster at the scoring band's edge and flee on volatility. From persisted books alone we can fingerprint it — depth that sits exactly at reward-qualifying widths, two-sided in the venue's required ratio, vanishing in event windows. Deliverables: **subsidy-adjusted depth** (a correction factor for every capacity estimate in this doc) and a category-level **subsidy-vs-toxicity map** (published reward rates vs L7's measured markout: any cell where subsidy > adverse selection is a genuinely farmable corner — and a finding either way). **Holes:** fingerprinting is heuristic — validate against reward-epoch boundaries (behavior should shift discontinuously at program changes, a built-in natural experiment); without minute-cadence sampling we see the pattern, not the uptime (bound claims accordingly, rule 2's cadence label).
**C11. Wash-fingerprint filter → organic-flow metrics.** ~25% of PM volume is wash-like [measured, Columbia]. Build a cheap tape filter (self-cross patterns, ping-pong at unchanged price, airdrop-era signatures) and re-express every flow statistic on the **organic remainder**. Then the creative step: test whether *wash share itself predicts* microstructure trouble — liquidity holes, spread blowouts, divergence persistence (farmers quote for points, not prices; their share should correlate with fake-depth fragility). Turns a data-cleaning chore into a forecasting variable. **Holes:** no ground-truth labels — validate the filter against the Columbia study's aggregate shares (should reproduce ~25% ± noise) before trusting per-market splits; Kalshi tape is anonymous, so this is PM-only (ANJ-fragile, capture-now per B3).
**C12. Shock-cluster dispersion (prediction-market stat-arb).** Many markets share one latent driver ("Fed cuts" moves twenty rate-adjacent markets). From retrospective 1-minute candles (keyless, both venues): cluster markets by co-movement, then measure **within-cluster lead-lag** — when the cluster's leader jumps, how long until the laggards reprice, and does a paper trade on the lag clear fees? Nobody publishes PM pairs-trading evidence; the half-life distribution is novel either way. **Holes:** correlation ≠ logical linkage — clusters must be validated against C7's registry or they're spurious (regime changes re-cluster); retrospective discovery + retrospective testing on the same window is the winner's curse again — discover on window 1, test on window 2 (rule 12's split, applied temporally).
**C13. Information cartography → a composite forecaster.** Generalize lead-lag into a directed graph: per category, which venue moves first (books→Kalshi on sports? PM→books on politics — [measured] for elections)? Then the product: a **synthetic composite price** weighting venues by measured category-level information share, graded by Brier score against settlements vs each single venue. If the composite wins, the instrument has produced something genuinely new: not a trade, but a *better probability* — publishable, and the natural core of any future "which venue do you trust" decision, including our own eligibility gates. **Holes:** information-share estimation at cron cadence is coarse (use C2 bursts + retrospective candles for the fine-grained cells); weights fit in-sample must be frozen before out-of-sample grading (rule 12); category coverage is uneven — report per-category n.

### Theme VI — Natural experiments

**C14. Regulatory event studies — we are inside the experiment.** The ANJ block (2026-07-16), the Ireland restrictions, US state carve-outs, Bet365's ANJ entry (2026-05-26): each is an exogenous shock to who can trade where, with known dates. We hold before/after data nobody else prioritizes: spreads, depth, divergence, ANJ-book overround around each event. Deliverables: did cutting France off from Polymarket move PM↔Kalshi spreads? Did Bet365's entry compress ANJ overrounds (L9's series is the control chart)? This is the academic-grade output the archive makes nearly free — and the one lane where our geographic handicap is the *methodological asset*. **Holes:** event studies need pre-period data — for future shocks this argues for **breadth of passive capture now** (aligned with B3); single-event inference is weak (frame as case studies with synthetic-control-style comparisons across unaffected categories, not p-values).

### Verify-first stub

**C16. Kalshi perpetual futures basis.** The 7.7.26 fee schedule introduced tiered perp fees [claimed, secondaries] — implying Kalshi now lists perpetuals. If real: internal basis trades (perp vs event-contract thresholds on the same underlying) and external basis (perp vs spot/CME) become measurable with zero new infrastructure. **Pin first:** confirm the product exists, its spec, funding mechanics, and data endpoints. Until pinned this is one row in the fact sheet, not a lane.

### C-series queue and pins

| Tier | Lane | Credits/mo | Power timeline | Pre-registered primary |
|---|---|---|---|---|
| **Next** | C3 implied-vs-realized ladders | 0 | immediate (retrospective) | pooled CRPS gap, unit = release |
| **Next** | C9 certainty complex (retro arms i–ii) | 0 | immediate (retrospective) | venue discount series; day-of-week term structure |
| **Next** | C1 weather settlement-native | 0 | ~1 season of dailies (~90 events/city) | after-fee P&L of "physically decided" signal; prelim-vs-final revision rate |
| **Next** | C2 burst microstructure | 0 (Kalshi/NWS/BLS legs); book legs share L5's line | per pre-registered burst count | event-time drift/reversion profile |
| **Later** | C5 ambiguity premium | 0 (rule-11 LLM budget) | scores now, disputes accrue | score→dispute/mismatch AUC, temporal split |
| **Later** | C12 cluster dispersion; C13 cartography; C10 subsidy map; C11 wash filter; C8 phrasing pairs; C6 hazard atlas; C7 partition registry; C15 combo calibration; C9-iii impatience LP | 0 | as tapes/registries accrue | as stated per lane |
| **Standing** | C14 event studies; C4 decomposition synthesis | 0 | per shock / after component lanes report | case-study protocol; accounting identity table |
| **Pinned** | C16 perps | — | — | existence + spec first |

**New pins for `data/verified_facts.md`:** NWS settlement semantics for KXHIGH-class series (climate report vs METAR — read the contract terms PDF at assets.kalshi.com, fetched-linked above); BLS/BEA/EIA API rate limits and revision conventions; Kalshi perps existence/spec; exact reward-band widths per venue for C10's fingerprint; PM data-api trade-tape field spec for C11. Interaction rule: C-lanes share rule 17 (no double-reporting) — C1/C9-iii extend L6, C2 extends L5, C10 informs L7, C4 consumes L2/L3/L5/C5 outputs; each pair's ownership boundary is written into the prereg.

---

## 9. H-series — hybrid strategies (v2.2)

Design brief: combine the components that survived the audit into composite lanes, structured around one axis — **frequency × size**. Everything measured so far sorts onto that axis: small edges (1–3¢) that recur daily and compound through capital velocity, versus large edges (3–6%+) that appear a few times a year and demand patience infrastructure. The practitioner evidence says both books exist but die differently: the small-edge book dies of fees, adverse selection, and velocity collapse; the big-edge book dies of settlement mismatch and of not being ready when the moment comes. Each hybrid below is built to survive its own death mode.

**Sequencing rule (binding):** hybrids are second-generation lanes. Each consumes the *pre-registered primary output* of one or more parent lanes and may not switch on before its parents report — stacking unvalidated screens multiplies garbage, not precision. Dependencies are listed per lane; rule 17 (no double-reporting) governs every parent–child boundary.

### The opportunity spectrum (what goes where)

| Cadence | Lane(s) | Typical gross edge | Lockup | Evidence grade |
|---|---|---|---|---|
| Multiple/day | L1 coherence residue; C12 laggard repricing; H9 quoting | sub-1¢–2¢ | minutes–hours | [measured], mostly fee-dead — the censored residue |
| Daily | H2 certainty conveyor (weather cities, finished games); L6/C9 yields | 1–4¢ on 96–99¢ | hours–days | [measured] cohorts pending; tail-bounded |
| Per release (~weekly) | H7 wings book (CPI, NFP, weather extremes) | small premia per bucket | days | [measured] retrospectively testable now |
| Weekly–monthly | H8 mismatch-priced spreads; L4 value cells; C8 phrasing pairs | 1–3¢ | days–weeks | [measured] parents, model layer new |
| Few/year | H5 dislocation book (election windows, panics); C14 shocks; L10 disputes | 3–6%+ | weeks–months | [measured] historically; conditionally fake-prone |
| Background | H6 subsidized patience (rewards income while flat) | ~subsidy rate | rolling | [claimed] ~10% APY corner; L7/C10 must confirm |

### H1. The two-book barbell (portfolio architecture)
**Combines:** everything, into two paper books with a pre-registered allocation split.
**Mechanism:** a **Premium Book** — many small, short-lockup, mutually diversified edges (H2 conveyor + H7 wings + L8-confirmed calibration tilts + H6 subsidy income), graded on edge-per-day-locked and compounding rate; and an **Event Book** — flat by default, deploying only on pre-registered rare triggers (H4/H5), graded on per-event capture and readiness (was the trigger armed when the event came?). The barbell claim to test: premium income is steady enough to "fund" the event book's option-like waiting, and the two books' P&L streams are near-uncorrelated — *measure that correlation*; if they spike together (both books are short the same settlement/oracle/regime factor), the barbell is an illusion (taxonomy class 11 at portfolio level).
**Holes:** book-level accounting invites gaming by reclassification — each lane is assigned to exactly one book at prereg, forever; "readiness" is measurable only via logged armed-state history (H4's tripwire log). Paper capital allocation is itself a parameter — freeze it (e.g., 80/20) before any comparison.

### H2. The certainty conveyor (small, often, compounding)
**Combines:** C1 (own the settlement feed) + L6 (tail rate) + C9-iii (impatience premium) + rule 6 velocity discipline.
**Mechanism:** a rolling daily ladder of self-verified near-certainties: enter *only* when our own feed says the outcome is physically locked (weather past peak-heat with high secured; games gone final; econ printed), enter as **resting maker bid at 95–98¢** to collect the impatience premium rather than lifting asks, exit at certification, roll the same capital into the next batch. Frequency is manufactured by breadth (weather cities × daily games × releases), compounding by short lockups: 1–2¢ on 97¢ over 1–2 days ≈ high-teens annualized *if* the tail rate and fill rate cooperate — which is precisely what the parents measure.
**Parents required:** L6 tail rate (bounds the loss term), C1 revision rate (bounds "physically locked"), L7 fill-bracket honesty (maker fills simulated conservatively).
**Holes:** capacity per market is hundreds of dollars, not thousands — this compounds a *small* base honestly or it lies; certification delays are operationally correlated (one venue ops backlog hits every rung of the ladder the same weekend — class 11); fee rounding at the extremes (B0's open pin) is 30–50% of the gross edge, so the pin blocks this lane's prereg; "decided" must come from the external feed, never the price (circularity ban, inherited from C9).

### H3. The coincidence detector (converting often-and-fake into rare-and-real)
**Combines:** L5 staleness stamps + C5 ambiguity score + C11 organic-flow share + depth validation (rule 5) + C13 lead-lag direction, stacked as a gate over every raw detection from L1–L3.
**Mechanism:** the fire-hose of detected "arbs" is mostly fake in five known, now-*measured* ways. Stack the five screens and let a detection graduate only if it passes all of them (fresh on both legs, unambiguous rules, organic flow, real depth, divergence *against* the lead venue rather than a laggard's stale echo). The deliverable is the **precision–frequency curve as screens stack**: raw detections/day vs settlement-verified real-arb rate at each gate depth. If the audit's thesis is right, precision multiplies while frequency collapses — quantifying exactly how rare "real" is, which is the number the whole project exists to produce.
**Parents required:** each screen's individual calibration (L5, C5, C11 primaries) — an unvalidated screen may not enter the stack.
**Holes:** screens are correlated (stale quotes are also thin quotes), so multiplicative precision claims need joint, not marginal, validation; the fully-gated sample will be tiny — report it as a rate with exact-binomial CIs, never as P&L; gate parameters frozen at prereg (rule 12's forking-paths ban applies with force here).

### H4. Tripwire escalation (pay attention cheaply, escalate rarely)
**Combines:** C2's calendar-burst capability + free-feed watchers + the credit budget discipline (B1).
**Mechanism:** the cron economy's answer to "bigger ones less often": background watchers that cost nothing (NWS anomaly flags, Kalshi halt/dispute flags, divergence z-score on persisted pairs, certainty-discount spikes from C9's stress series, regulatory calendar) each carry a pre-registered trigger; a firing trigger escalates that market cluster into burst mode (C2) and widens capture for a bounded window. Small cost always; full attention only when a precondition says the big one may be forming. Deliverable: trigger **precision/recall** — logged armed-states, fired-states, and post-hoc "did we catch it / what did we miss" audits against the settled record.
**Holes:** triggers defined after seeing an event are worthless — the registry is append-only with dated entries (same discipline as the prereg freeze); escalation that touches The Odds API draws from a bounded B1 line item with a monthly cap and an automatic stand-down; alert fatigue is measured (fires/month), not endured.

### H5. The dislocation option book (big, rare, adversely selected — priced accordingly)
**Combines:** L2's divergence series + H3's gate + C14's shock calendar + the 2024-election evidence (spreads to 6pp exactly when flow and event risk peaked [measured]).
**Mechanism:** stay flat; paper-deploy only when a mapped pair's divergence exceeds a pre-registered percentile of its own rolling history (e.g., >p99, regime-tagged) *and* survives the full H3 gate, holding to resolution. This is buying the blowout — the documented few-times-a-year regime where cross-venue spreads went 3–6%+.
**The honest core hole, stated as the lane's thesis:** divergence blowouts are *conditionally more likely to be fake* — spreads blow out precisely when settlement equivalence is genuinely in doubt (the Cardi B/Zelensky class), so the naive version of this lane is adverse selection at the event level. The lane therefore reports two numbers: capture on gated blowouts, and the counterfactual loss rate of ungated blowouts. If the second isn't dramatically worse, the gate adds nothing and the lane dies by its own prereg.
**Parents required:** L2 (divergence history + pooled mismatch rate), H3 (gate), C5 (ambiguity screen on the specific pair at trigger time).

### H6. Subsidized patience (get paid to wait; let fills be the alarm)
**Combines:** C10's subsidy-vs-toxicity map + L7's maker brackets + H4's escalation.
**Mechanism:** idle paper capital between events doesn't sit flat — it sits as reward-qualifying two-sided quotes in the categories C10 certifies as *subsidy > toxicity* (the practitioner-claimed ~10% APY corner: long-dated, low-vol, no news flow). Two income sources while waiting: the subsidy itself, plus the creative inversion — **a fill on our deep resting quote *is* information** (something moved through a quiet book), so every fill fires an H4 escalation on that cluster. Adverse selection, the maker's enemy, is repurposed as the event book's smoke detector.
**Holes:** the corner must be measured, not assumed — L7's bracket and C10's map are hard prerequisites, and the subsidy leg is unmeasurable without minute-cadence sampling (bound claims per L7); fills-as-signals only help if escalation is fast relative to the move (cron latency honestly disclosed — this detects *regimes*, not ticks); paper-fill realism brackets apply to the income claim too.

### H7. The wings insurance book (small premia, many independent underwritings)
**Combines:** C3's implied-vs-realized wings + L8's FLB/tail evidence + the event calendar + class-11 clustering control.
**Mechanism:** systematically paper-sell the overpriced tail buckets across many *independent* scheduled distributions — CPI wings, NFP wings, weather-extreme buckets across cities — collecting small premia per release, diversified across series whose surprises are (mostly) uncorrelated. It is an insurance book: frequent small wins, bounded per-release losses, graded like an underwriter — on calibration CIs per stratum, never on raw P&L (skew makes P&L a liar at feasible n; inherited from L8).
**Holes:** "independent" is the load-bearing word and it's partly false — inflation surprise hits CPI, rates, and Fed buckets in the same week (macro regime = one driver); the prereg caps same-driver exposure per window and reports realized cross-series loss correlation as a first-class output; selling tails inverts rule 8 legitimately (we're short the overpriced side) but oracle/dispute tails on subjective markets stay excluded — named-source numeric series only.

### H8. Mismatch-priced spreads (trade the fake arbs anyway — at the right price)
**Combines:** L2's pooled settlement-mismatch base rate + C5's ambiguity score + the REVIEW lane's quarantined detections.
**Mechanism:** the audit's deepest finding is that cross-venue "arbs" are short a hidden disagreement option. Once L2 and C5 produce P(mismatch | pair features, ambiguity score), that option has a price — so REVIEW-lane detections stop being binary discard/accept and become spread trades with an explicit expected value: divergence − P(mismatch)×loss-given-mismatch − fees. Paper-trade every REVIEW detection whose *priced* EV clears a frozen threshold; grade at settlement. If the model is honest, this converts the project's discard pile into its most original dataset — the first measured EV curve for "imperfect twins."
**Holes:** this is a pricing layer, not a whitelist bypass — rule 6 stands, everything stays REVIEW/paper, and no §18.2 shortcut follows from paper success; model risk is the whole risk (out-of-time validation split mandatory); loss-given-mismatch is bimodal (partial vs total), so the EV needs both terms estimated, not a blended constant; parents (L2 base rate, C5 calibration) must report first — this is the *last* lane in the dependency graph, and correctly so.

### H9. Composite-anchored quoting (small, continuous, alpha-fed)
**Combines:** C13's Brier-graded composite price + L7's maker brackets + C10's subsidy map.
**Mechanism:** the practitioner postmortem's verdict was "passive quoting without independent alpha is negative-EV." C13's composite — if it beats every single venue on Brier — *is* independent-ish alpha: quote around the composite instead of the local mid, in subsidy-positive categories, and let the measured information-share weights decide which venue's moves to respect and which to fade. The paper deliverable: markout of composite-anchored simulated quotes vs mid-anchored ones — does the alpha anchor flip the sign that killed naive MM?
**Holes:** everything inherits L7's paralyzed-quoter honesty (bounds, not points); if C13's composite doesn't beat single venues out-of-sample, this lane never exists (clean kill, by design); cross-venue anchoring needs both feeds live — ANJ fragility (B3/B4) is a standing threat to the PM leg.

### H-series queue

| Tier | Lane | Parents that must report first | Credits/mo |
|---|---|---|---|
| **Design now, run on parents' report** | H1 barbell accounting; H4 tripwire registry (definitions are append-only from today) | — (architecture) | 0 |
| **First wave** | H2 conveyor | L6, C1, B0 rounding pin | 0 |
| **First wave** | H7 wings book | C3, L8 | 0 |
| **Second wave** | H3 coincidence detector | L5, C5, C11 | 0 |
| **Second wave** | H6 subsidized patience | C10, L7 | 0 |
| **Third wave** | H5 dislocation book | L2, H3, C5 | 0 (bursts via H4's B1 line) |
| **Third wave** | H9 composite quoting | C13, L7, C10 | 0 |
| **Last** | H8 mismatch-priced spreads | L2 (base rate), C5 (calibrated) | 0 |

The dependency graph is the point: the Now-tier retrospective lanes (L6, L8, C3) were chosen in v2 because they unblock fastest — and it's now visible that they are also the parents of the first hybrid wave. Nothing in the H-series requires new data, new credits, or new venues; it requires the parents' numbers, in order.

---

## 10. Sources

v1 was overwritten in place (it was never committed); its full citation trail survives in the research-agent transcripts, and every load-bearing primary is retained here: [arXiv:2508.03474](https://arxiv.org/abs/2508.03474) ($40M/yr decomposition) · [arXiv:2601.01706](https://arxiv.org/pdf/2601.01706) (2–4% cross-platform) · [arXiv:2605.00864](https://arxiv.org/abs/2605.00864) (3.6s half-life) · [CEPR DP20631](https://cepr.org/publications/dp20631) (Kalshi makers/takers) · [Stanford adverse selection](https://law.stanford.edu/2026/04/21/adverse-selection-in-prediction-markets-evidence-from-kalshi/) · [Štrumbelj de-vig](https://www.sciencedirect.com/science/article/abs/pii/S0169207014000533) · [kacho.io post-mortem](https://kacho.io/polymarket-arbitrage-real-numbers) · [Kalshi fees](https://help.kalshi.com/en/articles/13823805-fees) · [Polymarket fees](https://help.polymarket.com/en/articles/13364478-trading-fees) · [Odds API docs](https://the-odds-api.com/liveapi/guides/v4/) · [Kalshi France restriction](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states) · [SX sunset](https://blog.sx.bet/news/sunsetting-the-sx-token-and-new-infrastructure-plans/).
