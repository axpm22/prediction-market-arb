# H-series — hybrid strategies

Standalone extraction of STRATEGIES.md §9 (canonical — if the two ever differ, STRATEGIES.md wins). 2026-07-28. Logic only — no code.

Design brief: combine the components that survived the audit into composite lanes, structured around one axis — **frequency × size**. Everything measured so far sorts onto that axis: small edges (1–3¢) that recur daily and compound through capital velocity, versus large edges (3–6%+) that appear a few times a year and demand patience infrastructure. The practitioner evidence says both books exist but die differently: the small-edge book dies of fees, adverse selection, and velocity collapse; the big-edge book dies of settlement mismatch and of not being ready when the moment comes. Each hybrid below is built to survive its own death mode.

**Sequencing rule (binding):** hybrids are second-generation lanes. Each consumes the *pre-registered primary output* of one or more parent lanes and may not switch on before its parents report — stacking unvalidated screens multiplies garbage, not precision. Dependencies are listed per lane; rule 17 (no double-reporting) governs every parent–child boundary. Lane references (L=audited survivors, C=creative extensions, B=blockers, rules) resolve in STRATEGIES.md.

## The opportunity spectrum (what goes where)

| Cadence | Lane(s) | Typical gross edge | Lockup | Evidence grade |
|---|---|---|---|---|
| Multiple/day | L1 coherence residue; C12 laggard repricing; H9 quoting | sub-1¢–2¢ | minutes–hours | [measured], mostly fee-dead — the censored residue |
| Daily | H2 certainty conveyor (weather cities, finished games); L6/C9 yields | 1–4¢ on 96–99¢ | hours–days | [measured] cohorts pending; tail-bounded |
| Per release (~weekly) | H7 wings book (CPI, NFP, weather extremes) | small premia per bucket | days | [measured] retrospectively testable now |
| Weekly–monthly | H8 mismatch-priced spreads; L4 value cells; C8 phrasing pairs | 1–3¢ | days–weeks | [measured] parents, model layer new |
| Few/year | H5 dislocation book (election windows, panics); C14 shocks; L10 disputes | 3–6%+ | weeks–months | [measured] historically; conditionally fake-prone |
| Background | H6 subsidized patience (rewards income while flat) | ~subsidy rate | rolling | [claimed] ~10% APY corner; L7/C10 must confirm |

## H1. The two-book barbell (portfolio architecture)
**Combines:** everything, into two paper books with a pre-registered allocation split.
**Mechanism:** a **Premium Book** — many small, short-lockup, mutually diversified edges (H2 conveyor + H7 wings + L8-confirmed calibration tilts + H6 subsidy income), graded on edge-per-day-locked and compounding rate; and an **Event Book** — flat by default, deploying only on pre-registered rare triggers (H4/H5), graded on per-event capture and readiness (was the trigger armed when the event came?). The barbell claim to test: premium income is steady enough to "fund" the event book's option-like waiting, and the two books' P&L streams are near-uncorrelated — *measure that correlation*; if they spike together (both books are short the same settlement/oracle/regime factor), the barbell is an illusion (taxonomy class 11 at portfolio level).
**Holes:** book-level accounting invites gaming by reclassification — each lane is assigned to exactly one book at prereg, forever; "readiness" is measurable only via logged armed-state history (H4's tripwire log). Paper capital allocation is itself a parameter — freeze it (e.g., 80/20) before any comparison.

## H2. The certainty conveyor (small, often, compounding)
**Combines:** C1 (own the settlement feed) + L6 (tail rate) + C9-iii (impatience premium) + velocity discipline.
**Mechanism:** a rolling daily ladder of self-verified near-certainties: enter *only* when our own feed says the outcome is physically locked (weather past peak-heat with high secured; games gone final; econ printed), enter as **resting maker bid at 95–98¢** to collect the impatience premium rather than lifting asks, exit at certification, roll the same capital into the next batch. Frequency is manufactured by breadth (weather cities × daily games × releases), compounding by short lockups: 1–2¢ on 97¢ over 1–2 days ≈ high-teens annualized *if* the tail rate and fill rate cooperate — which is precisely what the parents measure.
**Parents required:** L6 tail rate (bounds the loss term), C1 revision rate (bounds "physically locked"), L7 fill-bracket honesty (maker fills simulated conservatively).
**Holes:** capacity per market is hundreds of dollars, not thousands — this compounds a *small* base honestly or it lies; certification delays are operationally correlated (one venue ops backlog hits every rung of the ladder the same weekend — class 11); fee rounding at the extremes (B0's open pin) is 30–50% of the gross edge, so the pin blocks this lane's prereg; "decided" must come from the external feed, never the price (circularity ban, inherited from C9).

## H3. The coincidence detector (converting often-and-fake into rare-and-real)
**Combines:** L5 staleness stamps + C5 ambiguity score + C11 organic-flow share + depth validation (rule 5) + C13 lead-lag direction, stacked as a gate over every raw detection from L1–L3.
**Mechanism:** the fire-hose of detected "arbs" is mostly fake in five known, now-*measured* ways. Stack the five screens and let a detection graduate only if it passes all of them (fresh on both legs, unambiguous rules, organic flow, real depth, divergence *against* the lead venue rather than a laggard's stale echo). The deliverable is the **precision–frequency curve as screens stack**: raw detections/day vs settlement-verified real-arb rate at each gate depth. If the audit's thesis is right, precision multiplies while frequency collapses — quantifying exactly how rare "real" is, which is the number the whole project exists to produce.
**Parents required:** each screen's individual calibration (L5, C5, C11 primaries) — an unvalidated screen may not enter the stack.
**Holes:** screens are correlated (stale quotes are also thin quotes), so multiplicative precision claims need joint, not marginal, validation; the fully-gated sample will be tiny — report it as a rate with exact-binomial CIs, never as P&L; gate parameters frozen at prereg (rule 12's forking-paths ban applies with force here).

## H4. Tripwire escalation (pay attention cheaply, escalate rarely)
**Combines:** C2's calendar-burst capability + free-feed watchers + the credit budget discipline (B1).
**Mechanism:** the cron economy's answer to "bigger ones less often": background watchers that cost nothing (NWS anomaly flags, Kalshi halt/dispute flags, divergence z-score on persisted pairs, certainty-discount spikes from C9's stress series, regulatory calendar) each carry a pre-registered trigger; a firing trigger escalates that market cluster into burst mode (C2) and widens capture for a bounded window. Small cost always; full attention only when a precondition says the big one may be forming. Deliverable: trigger **precision/recall** — logged armed-states, fired-states, and post-hoc "did we catch it / what did we miss" audits against the settled record.
**Holes:** triggers defined after seeing an event are worthless — the registry is append-only with dated entries (same discipline as the prereg freeze); escalation that touches The Odds API draws from a bounded B1 line item with a monthly cap and an automatic stand-down; alert fatigue is measured (fires/month), not endured.

## H5. The dislocation option book (big, rare, adversely selected — priced accordingly)
**Combines:** L2's divergence series + H3's gate + C14's shock calendar + the 2024-election evidence (spreads to 6pp exactly when flow and event risk peaked [measured]).
**Mechanism:** stay flat; paper-deploy only when a mapped pair's divergence exceeds a pre-registered percentile of its own rolling history (e.g., >p99, regime-tagged) *and* survives the full H3 gate, holding to resolution. This is buying the blowout — the documented few-times-a-year regime where cross-venue spreads went 3–6%+.
**The honest core hole, stated as the lane's thesis:** divergence blowouts are *conditionally more likely to be fake* — spreads blow out precisely when settlement equivalence is genuinely in doubt (the Cardi B/Zelensky class), so the naive version of this lane is adverse selection at the event level. The lane therefore reports two numbers: capture on gated blowouts, and the counterfactual loss rate of ungated blowouts. If the second isn't dramatically worse, the gate adds nothing and the lane dies by its own prereg.
**Parents required:** L2 (divergence history + pooled mismatch rate), H3 (gate), C5 (ambiguity screen on the specific pair at trigger time).

## H6. Subsidized patience (get paid to wait; let fills be the alarm)
**Combines:** C10's subsidy-vs-toxicity map + L7's maker brackets + H4's escalation.
**Mechanism:** idle paper capital between events doesn't sit flat — it sits as reward-qualifying two-sided quotes in the categories C10 certifies as *subsidy > toxicity* (the practitioner-claimed ~10% APY corner: long-dated, low-vol, no news flow). Two income sources while waiting: the subsidy itself, plus the creative inversion — **a fill on our deep resting quote *is* information** (something moved through a quiet book), so every fill fires an H4 escalation on that cluster. Adverse selection, the maker's enemy, is repurposed as the event book's smoke detector.
**Holes:** the corner must be measured, not assumed — L7's bracket and C10's map are hard prerequisites, and the subsidy leg is unmeasurable without minute-cadence sampling (bound claims per L7); fills-as-signals only help if escalation is fast relative to the move (cron latency honestly disclosed — this detects *regimes*, not ticks); paper-fill realism brackets apply to the income claim too.

## H7. The wings insurance book (small premia, many independent underwritings)
**Combines:** C3's implied-vs-realized wings + L8's FLB/tail evidence + the event calendar + class-11 clustering control.
**Mechanism:** systematically paper-sell the overpriced tail buckets across many *independent* scheduled distributions — CPI wings, NFP wings, weather-extreme buckets across cities — collecting small premia per release, diversified across series whose surprises are (mostly) uncorrelated. It is an insurance book: frequent small wins, bounded per-release losses, graded like an underwriter — on calibration CIs per stratum, never on raw P&L (skew makes P&L a liar at feasible n; inherited from L8).
**Holes:** "independent" is the load-bearing word and it's partly false — inflation surprise hits CPI, rates, and Fed buckets in the same week (macro regime = one driver); the prereg caps same-driver exposure per window and reports realized cross-series loss correlation as a first-class output; selling tails inverts rule 8 legitimately (we're short the overpriced side) but oracle/dispute tails on subjective markets stay excluded — named-source numeric series only.

## H8. Mismatch-priced spreads (trade the fake arbs anyway — at the right price)
**Combines:** L2's pooled settlement-mismatch base rate + C5's ambiguity score + the REVIEW lane's quarantined detections.
**Mechanism:** the audit's deepest finding is that cross-venue "arbs" are short a hidden disagreement option. Once L2 and C5 produce P(mismatch | pair features, ambiguity score), that option has a price — so REVIEW-lane detections stop being binary discard/accept and become spread trades with an explicit expected value: divergence − P(mismatch)×loss-given-mismatch − fees. Paper-trade every REVIEW detection whose *priced* EV clears a frozen threshold; grade at settlement. If the model is honest, this converts the project's discard pile into its most original dataset — the first measured EV curve for "imperfect twins."
**Holes:** this is a pricing layer, not a whitelist bypass — rule 6 stands, everything stays REVIEW/paper, and no §18.2 shortcut follows from paper success; model risk is the whole risk (out-of-time validation split mandatory); loss-given-mismatch is bimodal (partial vs total), so the EV needs both terms estimated, not a blended constant; parents (L2 base rate, C5 calibration) must report first — this is the *last* lane in the dependency graph, and correctly so.

## H9. Composite-anchored quoting (small, continuous, alpha-fed)
**Combines:** C13's Brier-graded composite price + L7's maker brackets + C10's subsidy map.
**Mechanism:** the practitioner postmortem's verdict was "passive quoting without independent alpha is negative-EV." C13's composite — if it beats every single venue on Brier — *is* independent-ish alpha: quote around the composite instead of the local mid, in subsidy-positive categories, and let the measured information-share weights decide which venue's moves to respect and which to fade. The paper deliverable: markout of composite-anchored simulated quotes vs mid-anchored ones — does the alpha anchor flip the sign that killed naive MM?
**Holes:** everything inherits L7's paralyzed-quoter honesty (bounds, not points); if C13's composite doesn't beat single venues out-of-sample, this lane never exists (clean kill, by design); cross-venue anchoring needs both feeds live — ANJ fragility (B3/B4) is a standing threat to the PM leg.

## H-series queue

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
