# model_changes.md

Ten things the model does well, each with a concrete way to go further. The through-line:
the methodology discipline (append-only ledger, pre-registration, confirmed-not-optimistic
fills) is the strongest part. Most improvements below replace global constants with
per-sport / per-venue calibration drawn from the ~945k quotes already being stored but not
yet fully exploited.

## 1. Consensus de-vig is careful but single-method
**Does well:** Complete-outcome-set gating, median-then-renormalize, and line-aware totals
handling in `consensus_probabilities` (`pricing.py`) are well-reasoned.
**Could do better:** The live consensus uses proportional de-vig only, which biases
favorites/longshots. Per-book probabilities are persisted precisely so flags *can* be
re-graded under Shin/power/additive — yet nothing does. Compute the consensus under two
methods and report the disagreement as an error bar.

## 2. Per-venue fee modeling is correct but slippage is a flat constant
**Does well:** Splitting Kalshi's quadratic `p(1-p)` from Polymarket/SX linear fees
(`FEE_MODELS`, `all_in_fee_cost`) was a real fix.
**Could do better:** `taker_buffer` is one global constant added regardless of book depth —
thin books should cost more to cross than deep ones. Scale the buffer by observed depth
instead of applying a single half-cent everywhere.

## 3. The guaranteed-floor arb math is honest but capacity is top-of-book only
**Does well:** Returning the real cent-rounded worst-case floor and flagging
`rounding-broke-lock` (`equalized_arb_stakes`) is better than most retail tools.
**Could do better:** `capturable_lower_bound` takes only the min top-of-book depth x ROI —
it can't walk the ladder or model partial fills, so capacity collapses to a binary floor.
Storing full depth ladders would let you estimate realistic fillable size.

## 4. Match-sanity gates work but thresholds aren't sport-specific
**Does well:** Start-drift, same-outcome-spread, and leg-skew gates are the right defenses
against fake arbs.
**Could do better:** `match_suspect_prob_diff` and the drift limits are global constants —
soccer, MLB, and tennis have very different natural cross-venue spreads. Calibrate these
per-sport from your own match-confirmed history.

## 5. Quality labeling is principled but infers one-sided books indirectly
**Does well:** The append-only "label, never delete" approach with `suspect-edge` /
`thin-book` (`classify_quality`) is exactly right, and it caught the 545x contaminant.
**Could do better:** Empty/one-sided books are detected *implicitly* via the edge-magnitude
cap rather than checking book two-sidedness directly. An explicit "is there a real bid and
ask?" test would catch traps the edge cap misses.

## 6. Segment scoring uses confirmed fills but the normalization is arbitrary
**Does well:** Scoring on `pnl_confirmed` (not the optimistic mirage) and the
`INSUFFICIENT_DATA` tier are good discipline.
**Could do better:** `_sigmoid_norm` with a hardcoded `scale=0.1` saturates — a +12% and a
+45% ROI both map near 1.0, compressing the segments you most want to rank. The score also
carries no uncertainty: n=30 and n=300 at equal ROI differ only via the sample term. Add
shrinkage/CIs and calibrate the sigmoid scale to your ROI distribution.

## 7. The favorite-longshot study is rigorous but self-admittedly selection-biased
**Does well:** Binning on pre-fee cost and comparing realized win-rate to the *mean actual
cost* (not the midpoint) avoids the fee-masquerades-as-premium error (`bias.py`).
**Could do better:** The sample is filtered bets only, so it measures your filter's
calibration, not the market premium — flagged honestly in `SELECTION_CAVEAT`. Persisting and
binning a representative cross-section of settled raw quotes is the missing piece that would
make the signal actionable.

## 8. Settlement is append-only and reconciled but still single-source
**Does well:** Score-based settle plus the `reconcile_arb` venue-resolution cross-check
(handling VOID-refund and mismatch as a settler-error signal), and the MLB backfill that
rescues orphaned bets.
**Could do better:** Reconciliation is specced "not yet primary" — realized ARB P&L is still
copied from the provisional lock until a correction row arrives. Pulling each venue's actual
public resolution at settle time would close the largest unmeasured risk you name yourself.

## 9. The CLV gate is de-vig-agnostic but close-capture timing is unspecified
**Does well:** Gating EV on closing-line value rather than your own probability model
sidesteps de-vig bias entirely (`clv_points`).
**Could do better:** CLV depends on a clean "venue close mid," and a close captured a few
minutes early or late is noisy. Formalize the closing snapshot timing (and store several
pre-close marks) so CLV isn't hostage to when the last poll happened to land.

## 10. Exposure tracking exists but the cap and lock-duration are crude
**Does well:** Tracking total and per-venue committed stake — and recognizing you can't
instantly move USDC between venues — is a real portfolio-level fix most paper tools skip
(`compute_exposure`).
**Could do better:** `per_venue_cap` is one global number, when Kalshi USD, Polymarket USDC,
and SX balances are genuinely different pools; and exposure ignores *how long* capital is
locked, so a 14-day-settlement bet looks identical to a same-day one. Per-venue caps plus
duration-weighted exposure would match the real constraint already modeled in
`capital_charge`.

---

# Weaknesses

Ten substantive weaknesses in the model, ordered roughly by severity. Grounded in
`pricing.py`, `arbitrage.py`, `tracker.py`, `matching.py`, `settlement.py`, `venues/sx.py`.

## 1. ARB settlement never verifies the lock — it just re-books the entry P&L
`_append_arb_resolved_row` (`settlement.py`) fires whenever a final score exists and writes
`result='RESOLVED'` with the locked-at-entry `pnl_optimistic`/`pnl_confirmed` unchanged. It
never checks that each leg actually resolved the way the arb assumed (void/push/NO_CONTEST on
one venue, extra-innings totals, different oracles). Settlement-mismatch is the #1 risk the
spec is built around, yet for ARBs the "flagged vs realized" honesty metric agrees by
construction. The cross-check is deferred to a separate `reconcile` step that may never run.

## 2. P&L ignores executable depth
`equalized_arb_stakes` sizes legs off `bankroll × max_stake_pct`; depth enters only as a
non-binding `capturable:` note. A book that's $5 deep books the same dollar P&L as one that's
$500 deep, so the headline "how much would I have made" is uncapped by what could be filled.

## 3. The optimistic-vs-confirmed "mirage gap" is only populated for ARB
`_optimistic_pnl` returns `None` for everything but ARB, so EV (the higher-volume class)
carries empty optimistic P&L. And "confirmed" is just the price one poll (~60s) later, still
at top of book — not a fill/latency/queue model. The gap is computed where it's tiny
(1/0.96 vs 1/0.97) and absent where it would matter.

## 4. The reappearance rule inflates bet counts and cumulative P&L
"Absent for ≥1 poll → new bet" means a flickering quote becomes many independent "bets" from
one underlying mispricing. "$1 on every flag" oversamples persistent/oscillating edges, so
cumulative totals reward instability rather than distinct opportunities.

## 5. EV "truth" is de-vigged US sportsbook consensus, assumed sharper than the markets it grades
Calling Kalshi/Polymarket "+EV" relative to The Odds API consensus bakes in the assumption
that retail US books are better-calibrated than the prediction markets — frequently false.
When that's wrong the EV *sign* flips: you buy the side the model mislabels as cheap. A strong
modeling assumption presented as edge.

## 6. Naive multiplicative de-vig carries favorite-longshot bias straight into `p_hat`
`devig_book` normalizes 1/odds proportionally, which overstates favorites and understates
longshots versus true probability. EV and Kelly both inherit that bias. `bias.py` acknowledges
FLB exists but the consensus feeding decisions doesn't correct for it.

## 7. `event_key` date-bucketing reintroduces the boundary bug it claims to have fixed
`matching.py` removes the 6h bucket to avoid a seam, then keys on UTC *calendar date* —
recreating the identical failure at the 00:00 UTC seam. Late US games logged at 23:55 vs 00:05
UTC by two venues get different dates and never match, dropping arbs. Same-day doubleheaders
collapse to one key and lean on the start-drift gate, which *suspends* them — so legitimate
second-game arbs are suppressed.

## 8. Fees — first-order at sub-4% edges — run on unverified rates
`kalshi.fee_rate: 0.07` is a placeholder ("confirm in Phase 3"), and SX's `null` rate is
silently treated as `0` (just a `fee-rate-unverified` note) so the adapter still produces
tradeable-looking costs. An arb that only clears because SX fees were assumed zero is a phantom
arb, and fees move the decision at exactly these edge sizes.

## 9. "Left on the table" is unimplemented
It's an explicit §9 deliverable (filter-cost honesty), but `_daily_markdown` literally writes
"Not persisted yet." The model can't quantify what its own liquidity/depth/horizon filters
discard, so it can't tell whether the filters are conservative or throwing away the real edge.

## 10. DIVERGENCE bets are dead weight, and cross-venue depth is apples-to-oranges
Divergences are recorded with `stake=0` and never scored, so the "stat-arb fuel" never yields a
mean-reversion P&L or validates whether a spread is signal or a matching error. Separately, SX
`depth_usd` is the *maker's* remaining stake (`remaining/USDC_SCALE`), not the taker's
$-notional at the ask like Kalshi/Polymarket report — so `capturable_lower_bound`'s `min(depth)`
mixes inconsistent units across legs.

---

# EV-specific weaknesses

Ten things the model does poorly on the +EV path specifically (`pricing.py`,
`cli.py:detect_ev_from_consensus`, `sizing.py`, `scoring.py`, `bias.py`,
`experimental/ev/clv.py`, `exposure.py`), each with a fix. The chain is
`p̂` (consensus) → `EV = (p̂ − c)/c` → edge / Kelly; nearly every weakness is an error in
`p̂` or in treating `p̂` as exact, and it flows straight into staking.

## 1. De-vig method is biased toward favorites (proportional normalization)
`devig_book` (`pricing.py:31`) removes vig by proportional scaling `p̂ᵢ = (1/oᵢ)/Σ(1/oⱼ)` —
the crudest method, which systematically overprices favorites and underprices longshots vs
Shin/power/log methods. Since EV is "consensus minus ask," this biases every flag in a
predictable direction.
**Fix:** implement Shin's (or the power) method and pick the method empirically by which
minimizes Brier/log-loss on settled outcomes via `forecast/scoring.py`; make it a config knob.

## 2. Equal-weight median ignores book sharpness
`consensus_probabilities` (`pricing.py:51`) takes a plain median across books — a soft book
votes equally with a sharp one (Pinnacle/Circa). Equal weighting dilutes the only genuinely
predictive inputs; soft-book consensus is itself an exploitable mispricing, not truth.
**Fix:** weight books or anchor to a sharp subset; when only soft books are available, raise
`e_min` / lower trust for that market.

## 3. `p̂` is treated as a point truth — estimation uncertainty is ignored
`EV` and `kelly_f*` (`pricing.py:130`, `sizing.py:14`) use `p̂` as if known, but it's a median
of as few as 2–3 books. A 5-pt edge from 3 disagreeing books is treated like a 5-pt edge from
12 tight ones; `e_min` is a flat constant and Kelly is flat quarter-Kelly, so noisier consensus
→ more over-betting.
**Fix:** compute consensus dispersion (SE/IQR of the per-book probs already in `per_outcome`),
shrink the edge by a multiple of SE, gate on an **edge/SE ratio**, and carry SE into Kelly.

## 4. The headline edge metric `(p̂ − c)/c` explodes on cheap contracts
`expected_value` returns the ratio, stored as `Opportunity.edge`. A $0.02 contract with
`p̂=0.04` reads "100% EV" — exactly the contamination `quality.py` patches after the fact with
`suspect-edge`. The ratio is unstable where contracts are cheapest and shouldn't drive ranking
or sizing.
**Fix:** make `edge_points = p̂ − c` the primary stored edge everywhere; keep the ratio
display-only.

## 5. The measured favorite-longshot miscalibration is never fed back into `p̂`
`bias.py` produces a realized-vs-implied calibration curve — literally a correction function
for `p̂` — but `detect_ev_from_consensus` uses raw `p̂`. The blocker (sample is flagged-bets-only,
per `SELECTION_CAVEAT`) is a fixable data problem; SPEC §6 already persists every quote.
**Fix:** build a calibration set from settled **raw** quotes, fit isotonic/Platt, apply
`p̂_calibrated` before EV, re-validate with ECE/Brier tooling.

## 6. Slippage is a flat additive buffer, independent of depth/stake/volatility
`all_in_fee_cost` (`pricing.py:99`) adds a constant `taker_price_buffer: 0.005` regardless of
how much of the book you'd eat. It understates friction on thin books (where micro-EV lives and
edges look biggest) and overstates it on deep ones.
**Fix:** model slippage as a function of `stake/depth_usd` (linear or sqrt impact) plus a
volatility term, not a flat constant.

## 7. Stale-consensus edge isn't decayed — full edge counts up to the cutoff
A pregame flag against a consensus up to `consensus_pregame_max_age_hours: 3` old counts its
full edge right up to the cliff, then is binary-quarantined one minute later (`cli.py:520`).
Pregame information decays smoothly, not as a step.
**Fix:** decay the edge (and widen effective `e_min`) as a continuous function of consensus age;
calibrate the decay rate against observed per-sport line movement.

## 8. No correlation / portfolio view — EV exposure double-counts shared risk
`exposure.py` sums raw committed dollars only; no covariance anywhere. EV bets are correlated
(same game's moneyline + total, runline ≈ moneyline, sport-day news shocks), so independent
per-bet Kelly + a dollar sum over-allocates to what is one bet expressed many ways.
`_cheapest_quote_per_outcome` (`cli.py:457`) dedups only the identical outcome.
**Fix:** group same-event legs and cap combined stake; add a correlation haircut / portfolio
Kelly and track per-event and per-sport-day aggregate exposure.

## 9. CLV — the "prove edge before betting" gate — is measured ask-to-ask with fees
`experimental/ev/clv.py` compares the fee-loaded entry ask to a later fee-loaded effective cost
on the same side (`venue_close` returns `cost + fee_cost`). Proper CLV is vs the **no-vig
closing mid**; loading fees on both ends biases CLV toward zero and can hide or fake an edge —
and this gate decides whether EV is "real."
**Fix:** store the two-sided closing book, devig to a closing mid, compute
`clv = closing_mid − entry_no_vig_price`; keep fees in P&L, out of the CLV signal.

## 10. EV is the headline across 5 sports, but consensus exists for 2 — and is never validated per segment
`consensus_sports: [baseball_mlb, basketball_nba]` only, yet scope is 5 sports + non-sports, so
EV is absent for soccer/tennis/MMA or (for 3-way soccer) runs the most biased proportional devig
where it's least valid. `scoring.py` gates segments on ROI/win-rate, not **calibration** — a
segment can clear the ROI gate on variance while its `p̂` is badly calibrated.
**Fix:** require a passing calibration check (ECE/Brier-skill from `forecast/scoring.py`) before
trusting EV in a segment; broaden consensus with per-sport devig (3-way soccer) or explicitly
scope EV to covered, calibrated segments.

**Priority:** the deepest issues are #3 (no uncertainty on `p̂`), #1 (biased devig), and #5
(calibration measured but never applied) — together `p̂` is both biased and treated as exact,
and everything downstream inherits it. After those, fix #9 so the proof-of-edge gate actually
measures edge.

---

# Proposed changes — profit levers (ranked)

Reality check first, because it sets the order: the instrument's own headline finding is that
riskless cross-venue arb is ≈$0 (44 settled arbs, +$0.13 total; 10 of 43 flash-checked edges
vanished on a re-quote). Any "make it win" change that loosens the depth floor, one-sided-book
reject, or cross-venue REVIEW gate just re-manufactures the 545x contaminant the harness was
built to expose. So "consistently win" honestly means *find where real, repeatable edge lives*,
not make the number bigger. These levers are ordered by risk-adjusted promise.

## Lever A — Shin/power de-vig as a *selector*, then CLV-grade the subset (do first)
**Why first:** changes the conclusion using data already on disk (10,742 sportsbook quotes,
1,169 EV flags), adds zero new betting risk, and directly attacks audit items #1/#3/#6 above.
The current CLV gate is de-vig-agnostic (entry cost vs venue close), so de-vig can't change a
flag's CLV — but it *can* change which flags get selected. The real question: is a less-biased
de-vig a better selector of CLV-positive flags?
**Build:**
1. Add `devig_book_shin` and `devig_book_power` to `pricing.py` beside the proportional
   `devig_book`, with a `method` dispatcher. Internal float root-finder (bisection: power solves
   Σ rᵢ^k = 1; Shin solves Σ pᵢ(z) = 1), Decimal output via `q6` + residual fix, matching the
   existing renormalization pattern. Exact unit vectors: symmetric (1.91, 1.91) → (0.5, 0.5) for
   all three methods; proportional (1.50, 2.86, 6.00) → (0.5635, 0.2956, 0.1409) unchanged;
   property test that Shin/power assign a *lower* prob to the longshot than proportional.
2. Thread a `method` arg through `consensus_probabilities` (default proportional → no behavior
   change).
3. New `arb/analysis/devig_regrade.py`: for each method, recompute consensus from the oddsapi
   archive, re-grade each stored EV flag's `edge_points = p̂ − c`, re-bucket, and CLV-grade the
   subset passing that method's `e_min`. Report per method: n, mean edge, mean CLV, bootstrap CI,
   positive rate. State the selection caveat loudly (flags were *originally* selected under
   proportional ≥ ev_threshold, so this is an in-sample re-grade, not a full replay — a full
   replay from raw quotes is the follow-up).
**Win condition:** if any method surfaces a segment whose mean CLV clears the pre-registered bar
(CI excludes 0, n ≥ 40), that's a genuine, defensible unlock — the first proven edge in the repo.
If none do, the EV thesis is further weakened honestly, also a result.

## Lever B — FLB calibration + fractional-Kelly sizing on confirmed bands
**Why:** the favorite-longshot premium is the most decades-persistent edge in the literature and
is *directional* (no cross-venue settlement risk), so it sidesteps the mirage entirely. This is
the single most likely *persistent* money-maker in the codebase — but it has variance, so
"consistent" means over hundreds of bets, not per day.
**Build:** close the `SELECTION_CAVEAT` gap in `bias.py` by binning a representative cross-section
of settled *raw* quotes (SPEC §6 already persists them), not flagged-bets-only. Fit bands before
forward data (pre-registration already frozen). Add fractional-Kelly sizing (¼–½) on bands that
clear `flb_min_edge` with ≥ `min_samples_per_band`. Feed the fitted calibration back into `p̂`
(audit EV-#5) so EV detection uses `p̂_calibrated`.
**Gate:** ≥200 settled bets/band before any band is actionable — this is data-accumulation-bound,
so start the raw-quote binning now and let n build.

## Lever C — Widen the negRisk basket net (cleanest near-riskless construct)
**Why:** intra-Polymarket basket arb settles on one venue against one oracle → the entire
settlement-mismatch failure mode that kills cross-venue arb is gone. It's the only construct here
that can be near-riskless *and* real. S1 found 0 arbs on 32 events (ΣYES ≈ 1.008) — so the work
is coverage, not detection cleverness.
**Build:** expand `negrisk.py` event coverage; tighten polling around event creation and
resolution windows (when baskets are briefly mispriced); lower the liquidity floor *for this
single-settlement construct only* (justified because there's no cross-venue leg risk). Report the
flag-lifetime distribution (how fast baskets close) as a result in itself.

## Lever D — Execution honesty (the ceiling nobody can code around)
**State plainly in the model's own docs:** the 33 VERIFIED arbs are real but sub-1% and
depth-capped. Capturing them consistently is an *execution* problem (latency, resting maker
orders where you lead — see the unconfirmed lead-lag hint), which a public-read paper harness
structurally cannot validate. So "consistently win" at scale requires either real capital +
low-latency execution, or accepting directional EV/FLB risk the CLV gate hasn't yet justified.
No detector change removes this ceiling; pretending otherwise is how the mirage returns.

**Order to execute:** A (now, unlocks a proven signal or kills it cheaply) → C (start live
coverage, near-riskless) → B (start raw-quote binning so n accrues) → D is a documentation/
expectations fix to make alongside A.

---

# Review pass 3 — additional improvements

Weighted toward model logic. Calibrated to current state (capital charge is now wired into
arbs, quote/`flat_bets` indices exist, fee `basis` split is in).

## 1. Upgrade the de-vig method
`devig_book` uses proportional normalization (divide each implied prob by the overround) —
the crudest devig, and it systematically misprices favorites vs longshots. Since the
consensus fair value is the foundation of every EV edge, this single choice decides whether
an "edge" is real or a devig artifact. Implement Shin (or power/logarithmic) devig and pick
the winner empirically against the FLB calibration curve.

## 2. Weight the consensus by book sharpness, don't take a plain median
`consensus_probabilities` takes an unweighted median of de-vigged probs across books, so a
soft book counts the same as Pinnacle/Circa. Weight by sharpness (or at least by tighter
overround), and record the cross-book dispersion as a confidence signal — a wide
disagreement across books should down-weight or flag the resulting EV, not be discarded by
the median.

## 3. Extend the capital charge to EV
`is_economic`/`capital_charge` is now applied to arbs (`arbitrage.py`), but the EV path has
no time-value gate — and EV ties up the $1 until settlement, up to `max_days_to_settlement`
(14 days), whereas arbs lock at entry. A 5% EV settling in 14 days is worse than one
settling in 2 hours; rank and gate EV on edge-per-day held, reusing the function already
present.

## 4. Model push/draw mass in EV value
EV is `(p−c)/c`, which treats every contract as a clean binary, but settlement already
recognizes VOID on integer-line totals. A contract near an integer total (or a 3-way market
with real draw mass) carries probability that returns stake rather than paying out, lowering
true EV. Fold push/draw probability into the EV calc for those market types.

## 5. Reject implausible odds and per-book outliers before the median
The only input sanity check is `raw_price <= 0`. A single mis-scaled or stale book (e.g.,
odds of 1.001 or 60.0 from a data glitch) flows straight into the consensus median. Add
implied-probability bounds and drop per-book outliers that sit too far from the cross-book
median before computing consensus.

## 6. Treat fees as an uncertainty band and hard-gate when stale
Fees set every `effective_cost`, so a wrong or stale fee silently shifts every edge, yet
`fee_stale_days` only emits a load-time warning. Carry a fee uncertainty band (like
`taker_price_buffer`) into a conservative effective cost, and when a fee is past
`fee_stale_days`, block betting/keep-status for that venue rather than just printing a badge.

## 7. Enforce dedup and poll atomicity at the DB layer
The fingerprint indices are non-unique and dedup is done as SELECT-then-INSERT (`btc_arb`,
`whales`, the tracker reappearance logic), which races across the many concurrent processes
(watch/scan/closing/ev-exit/hot/focus). Add UNIQUE constraints so the database enforces "one
open bet per fingerprint," and wrap a poll's quote+opportunity+finish writes in one
transaction so a crash can't leave a half-written poll (they're currently three separate
connections).

## 8. Consolidate the "latest lifecycle row per fingerprint" logic
It's reimplemented in `tracker._active_rows`, `settlement_health._active_sports_bets`,
`ev_exit`, and the CLV reader — and they've already diverged on whether `EXITED` counts as
terminal, which is exactly how the EV double-settle hole crept in. One shared helper with one
definition of "terminal" removes that whole class of bug.

## 9. Remove look-ahead from the gates
The segment scorer (`scoring.py`) and FLB calibration (`bias.py`) both fit thresholds and
then apply them over the *same* full settled sample, so segments get blocked/trusted and
bands get labeled on in-sample data. Use a walk-forward split (fit on a past window, apply
forward) or extend the pre-registration discipline already used in the validation playbook to
these gates, or you'll trust segments that only looked good in hindsight.

## 10. Qualify whales on realized ROI, not hit rate
In `whales.wallet_stats`, `qualified` keys off `win_rate` (plus trade count and category
share) and ignores `pnl_usd`, even though the module's own thesis is that a high win rate can
be right-but-unprofitable (one leg of a hedge, zombie-order inflation). Qualify on realized
ROI so the copy-trade study tests the thing it claims to.
