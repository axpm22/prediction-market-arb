# VALIDATION_PLAYBOOK.md — turning a sample result into a proven edge

This is the step-by-step method for going from *"the diagnostic found a positive slice"*
to *"I can defend this as a real edge in an interview."* It exists because a positive
realized P&L is **not** the same thing as an edge, and the difference is the entire
credibility of the project.

---

## 0. The confusion this resolves: losing bets vs. an uncertain average

A +EV strategy loses individual bets constantly — that is normal variance and is fine.
"Negatives are okay as long as there are more positives" is the right instinct about
*individual outcomes*. But the **"noise" verdict in the diagnostic is not about individual
bets** — it is about whether the **average** (the expectation itself) is reliably above zero.

Two different questions:

1. *Do some bets lose?* → Yes, always. Irrelevant to edge.
2. *Is the true mean per-bet P&L > 0?* → This is the edge question, and right now the data
   can't answer it.

**The coin test.** A fair coin paying even money has a true edge of exactly **zero**. Flip it
267 times at $1 and you will almost never land on $0 — variance alone leaves you ~±$15. So
"+$13 over 267 bets" is *exactly what a zero-edge process produces routinely*. A confidence
interval that straddles zero means: *your results are statistically consistent with having no
edge at all.* The losing bets are not the problem; the problem is the average is inside the
luck band.

**In win-rate terms.** At ~$0.50 contracts, break-even is ~50% (plus fees). You are at 50.2%
over 267 bets, where the uncertainty on the rate is ≈ ±6%. Your *true* win rate could be 44%
or 56%. "More wins than losses in this sample" is not proof the true rate beats break-even.

The cure is not a mindset about losses. It is **a bigger edge, a bigger sample, or both —
proven on data the rule has never seen.**

---

## 1. What counts as a proven edge (the bar)

A strategy has a demonstrated edge when:

- its **mean per-bet P&L is positive**, AND
- the **95% confidence interval on that mean excludes zero**, AND
- this holds on **out-of-sample** bets (data collected *after* the rule was fixed), AND
- the sample is **large enough** that the result isn't a small-n fluke.

All four. Drop any one and an interviewer will (correctly) say "that's noise."

---

## 2. Why +$13 isn't proof yet (the actual numbers)

From your ledger, per-bet P&L has standard deviation **SD ≈ $1.06** and mean **≈ $0.05**
(5% ROI flat-$1). The signal-to-noise per bet is tiny:

```
t = mean / (SD / sqrt(N)) = 0.05 / (1.06 / sqrt(267)) ≈ 0.77
```

You need t ≥ ~1.96 for the 95% CI to clear zero. You're at 0.77. That is why the diagnostic
prints `noise`. Nothing is wrong with the strategy *or* broken in the code — you simply do
not have enough evidence yet.

---

## 3. How many bets you actually need (power)

To make a CI of a given edge exclude zero at 95%:

```
N  ≈  ( 1.96 * SD / mean )^2          # SD ≈ 1.06 for your $1 binary bets
```

| True edge (per $1 bet) | ≈ ROI | Bets needed (N) |
|---|---|---|
| $0.05 | 5%  | **~1,700** |
| $0.08 | 8%  | ~675 |
| $0.10 | 10% | ~430 |
| $0.15 | 15% | **~190** |
| $0.20 | 20% | ~110 |

Two huge implications:

1. **A thin 5% edge is brutal to prove** — ~1,700 *filtered* settled bets, and since your
   filter keeps ~40% of flags, that's ~4,000+ raw flags. Months of data.
2. **The filtered slice is the *provable* path.** Your KEEP rule (mid-price, low flagged
   edge, confirmable) showed ~+$0.15/bet (~15% ROI). If that's real, it needs only **~190
   bets** to prove — achievable in weeks. *Chase the bigger, cleaner edge, not the thin
   blended one.*

This is the single most important strategic takeaway: **don't try to prove the 5% average —
try to prove the 15% filtered slice, because it's reachable.**

---

## 4. The procedure (do these in order)

### Step 1 — Freeze the rule (pre-register) BEFORE new data
Append a dated, immutable block to `PREREGISTRATION.md`. It must specify, exactly:
- the **filter** (e.g. `cost ∈ [0.40, 0.75] AND flagged_edge < 0.02 AND confirmable`),
- the **metric** (mean per-bet P&L, flat-$1, confirmed fills),
- the **success threshold** (95% bootstrap CI lower bound > 0),
- the **target N** (from the table above, e.g. 190),
- the **kill criteria** (below),
- the **date** you froze it.

Why first: if you pick the rule *after* seeing the data (as we just did by slicing), the
positive slice is partly cherry-picked. Pre-registration converts a hypothesis into a test.

### Step 2 — Collect only forward data
Run `watch` as normal. **Only bets placed on/after the freeze date count.** The in-sample
bets that *generated* the hypothesis are spent — they can never be evidence for it again.
The diagnostic supports this directly:

```
python -m arb.analysis.diagnose data/arb.sqlite --kind EV --since 2026-06-22
```

`--since <freeze-date>` scores only the forward bets.

### Step 3 — Account for multiple comparisons
We tested ~15 slices; by chance alone, ~1 will look "positive" at 95% even with zero true
edge. So **forward-test only ONE or two pre-registered rules**, not all the slices that
looked good. Testing one rule on fresh data is clean; re-mining the same data for the best
slice is p-hacking.

### Step 4 — Settle, score, decide
When N forward bets have settled, read the diagnostic's verdict on the `--since` set:
- **`SIGNAL+`** (CI lower bound > 0 at N ≥ target) → edge demonstrated. Write it up.
- **`noise`** (CI still straddles zero) → not proven; either keep collecting toward a larger
  N or conclude the edge is too thin to establish.
- **`SIGNAL-`** (CI upper bound < 0) → the rule is a net loser; kill it.

### Step 5 — Kill fast (don't bleed while testing)
Pre-commit to stopping early if the rule is clearly wrong (lesson from the 0-32 weather
postmortem — they waited 32 trades when 10 was enough):
- kill at **K consecutive losing bets** (e.g. K = 10), or
- kill if at the **halfway N** the point estimate is negative, or
- kill if the CI upper bound drops below your target edge.

### Step 6 — Walk forward / re-validate
Markets change. Even a proven rule should be re-checked on each new block of data
(walk-forward). A rule that was `SIGNAL+` last quarter and `noise` this quarter has decayed —
report that honestly rather than assuming permanence.

---

## 5. Concrete pre-registration block for your filtered rule

Paste into `PREREGISTRATION.md` (fill the date), then do not edit it:

```
- YYYY-MM-DD — H-EVFILTER (frozen before forward data).
  Rule: among EV flags, KEEP only cost ∈ [0.40, 0.75] AND flagged_edge < 0.02
        AND the flag was confirmable (seen on >=2 consecutive polls).
  Sizing for the test: flat $1, confirmed-fill prices.
  Hypothesis: mean per-bet P&L of the KEEP set is > 0, with a bootstrap 95% CI
        excluding 0, over N >= 190 forward settled bets.
  Do NOT size by flagged-edge magnitude (it is anti-predictive above ~2% in-sample).
  Kill criteria: 10 consecutive losing KEEP bets, OR CI upper bound < 0 at N >= 95,
        OR point estimate negative at N >= 95.
  Rationale: in-sample the KEEP set was 56% WR / +$0.15 per bet; the sub-$0.40 and
        flagged_edge>0.02 bands were net-negative and are excluded.
```

---

## 6. What the code already gives you, and the one gap

`arb/analysis/diagnose.py` already provides: per-slice bootstrap CIs, SIGNAL/NOISE/
INSUFFICIENT verdicts, a candidate-rule evaluator, a chronological out-of-sample split, and
the `--since` forward-test filter.

The remaining gap is **enforcement at bet-creation time**, which belongs in BUILD_PLAN
Phase 1 + Phase 2: encode the frozen filter so forward EV flags are tagged KEEP/DROP as they
are recorded, and surface the KEEP set as its own segment in the daily report. Then the
forward test runs itself, and the daily report tells you the moment the CI clears zero — or
the kill criterion trips.

---

## TL;DR
- Losing bets are fine; an unproven *average* is not.
- +$13 over 267 bets is consistent with **zero** edge (it's inside the luck band).
- Prove the **15% filtered slice** (~190 bets), not the **5% blend** (~1,700 bets).
- Freeze the rule → collect only forward data → score with `--since` → SIGNAL+ or kill.
- One pre-registered rule, out-of-sample, with kill criteria. That's a defensible result.
