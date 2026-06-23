# Where the EV model is weak — 10 problems and fixes

Scope: the +EV path only (`pricing.py`, `cli.py:detect_ev_from_consensus`, `sizing.py`,
`scoring.py`, `bias.py`, `experimental/ev/clv.py`, `exposure.py`). The arb/divergence
math is sound; the EV machinery is where the real estimation risk lives, because EV is
the one class that bets on a *predicted* probability rather than a locked spread.

The whole chain is: `p̂` (consensus) → `EV = (p̂ − c)/c` → edge / Kelly. Almost every
weakness below is an error in `p̂` or in how its uncertainty is handled — and those errors
flow straight into staking, because `p̂` is treated as truth.

---

## 1. De-vig method is biased toward favorites (proportional normalization)

`devig_book` (`pricing.py:31`) removes vig by simple proportional scaling:
`p̂ᵢ = (1/oᵢ)/Σ(1/oⱼ)`. This is the crudest of the standard methods and it is *known* to
mis-distribute the margin: it removes vig in proportion to implied probability, which
systematically **overprices favorites and underprices longshots** relative to Shin's
model, the power/logarithmic method, or the "balanced book" method. For a system whose
entire edge is "consensus minus ask," a biased `p̂` biases every single EV flag in a
predictable direction.

The irony: `bias.py` exists specifically to document the favorite-longshot bias, while the
devig method bakes in a *different* distortion of the same axis.

**Fix:** implement Shin's method (or at minimum the power method) and choose the devig
method empirically by which one minimizes Brier/log-loss on settled outcomes — you already
have `forecast/scoring.py` to score it. Make the method a `config.yaml` knob and A/B it.

---

## 2. Equal-weight median ignores book sharpness

`consensus_probabilities` (`pricing.py:51`) takes a plain **median across books**. Every
book votes equally — a soft, recreational book counts the same as a sharp one. The
literature is unambiguous that a small number of sharp books (Pinnacle, Circa) are far
more predictive than the recreational field, and that the *consensus of soft books* is
itself an exploitable mispricing, not a truth signal. Equal-weight median dilutes the only
genuinely sharp inputs into noise.

**Fix:** weight books (or anchor to a sharp subset) when forming `p̂`; expose the weights
in config. If only soft books are available for a market, lower trust / raise `e_min` for
that market rather than treating the median as gospel.

---

## 3. `p̂` is treated as a point truth — estimation uncertainty is ignored

`EV = (p̂ − c)/c` and `kelly_f* = (p̂ − c)/(1 − c)` (`pricing.py:130`, `sizing.py:14`) use
`p̂` as if it were known. But `p̂` is a median of as few as **2–3 books**
(`min_books_for_consensus: 3`, totals `: 2`). A 5-point edge from 3 wildly-disagreeing
books is treated identically to a 5-point edge from 12 tightly-clustered books. The edge
floor `e_min` is a flat constant (`ev.e_min: 0.05`) and Kelly is flat quarter-Kelly
regardless of dispersion — so the noisier the consensus, the more the model over-bets.

**Fix:** compute consensus dispersion (SE/IQR of the per-book de-vigged probs you already
gather in `per_outcome`), then (a) shrink the edge by a multiple of the SE, and (b) gate on
an **edge/SE ratio**, not a flat point threshold. Carry SE into Kelly so noisy `p̂` shrinks
the stake automatically.

---

## 4. The headline edge metric `(p̂ − c)/c` explodes on cheap contracts

`expected_value` returns the **ratio** `(p̂ − c)/c`, and it's stored as `Opportunity.edge`.
On a $0.02 contract with `p̂ = 0.04` that ratio is 1.0 ("100% EV"). This is exactly the
"four-digit edge" contamination that `quality.py` has to patch *after the fact* with a
`suspect-edge` flag. The ratio is unstable precisely where contracts are cheapest, and it
shouldn't drive ranking or sizing at all.

The code half-fixed this — `quality.py` and ranking now use `edge_points = p̂ − c` — but
the ratio is still the stored headline `edge` and still the thing called "EV."

**Fix:** make `edge_points` (probability points) the primary, stored edge everywhere; keep
the ratio as a display-only ROI figure. Nothing downstream (quality, ranking, Kelly) should
ever consume the ratio.

---

## 5. The measured favorite-longshot miscalibration is never fed back into `p̂`

`bias.py` produces a per-price-band calibration curve: realized win rate vs implied
probability. That curve *is* a correction function for `p̂` — it's the market's measured
miscalibration. Yet `detect_ev_from_consensus` uses raw `p̂` with no recalibration. The
model measures the bias and then ignores it when pricing.

The blocker is real and the code is honest about it (`SELECTION_CAVEAT`): the sample is
flagged-bets-only, so the curve reflects the filter, not the population. But that's a
fixable data-collection problem, and SPEC §6 already says you persist *every* quote.

**Fix:** build a representative calibration set from settled **raw quotes** (not just
flagged bets), fit isotonic regression or Platt scaling, and apply it as `p̂_calibrated`
before computing EV. Re-validate with the Brier/ECE tooling already in `forecast/scoring.py`.

---

## 6. Slippage is a flat additive buffer, independent of depth, stake, and volatility

`all_in_fee_cost` (`pricing.py:99`) adds a constant `taker_price_buffer: 0.005` to every
contract regardless of how much of the book you'd eat, how thin it is, or how volatile the
line is. Real slippage scales with stake/depth. This **understates** friction on thin books
(where the micro-EV path lives, and where edges look biggest) and **overstates** it on deep
liquid ones — biasing EV in opposite directions exactly where it matters most.

**Fix:** model slippage as a function of `stake / depth_usd` (a simple linear or square-root
impact term) plus a volatility/recency component, instead of a flat constant. The micro-EV
path especially needs depth-aware friction.

---

## 7. Stale-consensus edge isn't decayed — full edge counts up to the cutoff

A pregame flag is computed against a consensus that can be up to
`consensus_pregame_max_age_hours: 3` old (8h in-play). The code correctly *tags*
`stale-consensus` and quarantines it (`cli.py:520`), but the **edge magnitude is not
decayed**: a flag against a 2h59m-old line counts its full edge right up to the cliff, then
is binary-quarantined one minute later. Pregame lines move continuously; the information
decays smoothly, not as a step.

**Fix:** decay the edge (and widen the effective `e_min`) as a continuous function of
consensus age — e.g. multiply edge by an exponential staleness factor — rather than a hard
binary cutoff. Calibrate the decay rate against observed line movement per sport.

---

## 8. No correlation / portfolio view — EV exposure double-counts shared risk

`exposure.py` sums raw committed **dollars** only; there is no covariance anywhere
(`grep correlat` finds nothing in the EV path). But EV bets are heavily correlated: the same
game's moneyline and total move together, a team's runline and moneyline are near-redundant,
and an entire sport-day shares weather/injury/news shocks. Independent per-bet Kelly plus a
simple dollar sum **systematically over-allocates** to what is really one bet expressed many
ways, and the "expected value of the book" ignores the variance that correlation adds.

`_cheapest_quote_per_outcome` (`cli.py:457`) dedups only the *identical* outcome, not
correlated ones.

**Fix:** add correlation-aware exposure caps — at minimum group same-event legs and cap
combined stake; better, a portfolio-Kelly / correlation-haircut on simultaneous correlated
positions. Track per-event and per-sport-day aggregate exposure, not just per-venue dollars.

---

## 9. CLV — the "prove edge before betting" instrument — is measured ask-to-ask with fees

`experimental/ev/clv.py` is the system's central proof-of-edge gate, but it compares the
**fee-loaded entry ask** to the venue's later **fee-loaded effective cost** on the same side
(`venue_close` returns `cost + fee_cost`, docstring admits it's "the venue price proxy").
Proper CLV is measured against the **no-vig closing mid**, not a fee-loaded same-side ask.
Loading fees on both ends and comparing ask-to-ask biases CLV toward zero and can hide a
real edge (or, with a one-sided book, fake one). Since this gate decides whether EV is "real,"
a biased CLV propagates into the go/no-go decision for the entire strategy.

**Fix:** record the venue's two-sided closing book, devig it to a closing mid, and compute
`clv = closing_mid − entry_no_vig_price`. Keep fees in the P&L ledger, but out of the CLV
signal.

---

## 10. EV is the headline across 5 sports, but consensus exists for only 2 — and is never validated per segment

`consensus_sports: [baseball_mlb, basketball_nba]` — consensus is only fetched for MLB and
NBA, yet the project's scope is 5 sports plus non-sports. So EV either can't be computed for
soccer/tennis/MMA, or (worse, for 3-way soccer) would run the *most* biased proportional
devig on the markets where it's least valid. Meanwhile `scoring.py` gates segments on **ROI**
and win rate, not on **calibration** — a segment can clear the ROI gate on variance while its
`p̂` is badly calibrated, which is the thing that actually has to be true for EV to work.

**Fix:** before trusting EV in a segment, require a passing **calibration** check (ECE /
Brier-skill from `forecast/scoring.py`), not just positive ROI. Either broaden consensus
coverage with per-sport devig handling (3-way for soccer) or explicitly scope EV to the
segments where consensus is both available and calibrated, and say so in the report.

---

## Themes / priority

The deepest single problem is **#3 (no uncertainty on `p̂`)** combined with **#1 (biased
devig)** and **#5 (calibration never applied)** — together they mean the central quantity
`p̂` is both biased and treated as exact, and everything downstream inherits that. If you fix
only three things: (1) calibrate/devig `p̂` properly and validate it with the Brier tooling
you already have, (2) propagate `p̂`'s estimation error into the edge gate and Kelly, and
(3) fix the CLV signal to no-vig mids so your proof-of-edge gate is actually measuring edge.
