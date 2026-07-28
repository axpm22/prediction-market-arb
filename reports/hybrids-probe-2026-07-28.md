# H-series test — 2026-07-28

Tested `HYBRIDS.md` (extraction of STRATEGIES.md §9) against the L-series
results measured 2026-07-27 and the corpus built for them.

## 1. Sequencing audit — the binding result

HYBRIDS.md §7: *"hybrids are second-generation lanes... may not switch on
before its parents report."* Applying that rule literally:

| lane | parents ready | blocked on |
|---|---|---|
| H1 two-book barbell | n/a | architecture only, no primary to consume |
| H2 certainty conveyor | 1/3 | C1, B0 |
| H7 wings book | 1/2 | C3 |
| H3 coincidence detector | 1/3 | C5, C11 |
| H6 subsidized patience | 1/2 | C10 |
| H5 dislocation book | 0/3 | L2, H3, C5 |
| H9 composite quoting | 1/3 | C13, C10 |
| H8 mismatch-priced spreads | 0/2 | L2, C5 |

**0 of 7 hybrids can switch on.** Every blocking parent is either a C-series
lane (all unmeasured — the C-series queue lists them Next/Later) or B0, whose
fee-rounding granularity is still OPEN in the §7 fact sheet. L2 is reported
but not decision-grade (41 pairs, 0 mismatches, <=7.3% bound).

The dependency graph is doing its job: the H-series correctly refuses to run
on unvalidated screens. Nothing below changes that; the probes test whether
*already-measured* parents falsify a hybrid's mechanism regardless.

## 2. Probe — H2 / C9-iii: does maker toxicity invert in the certainty band?

C9-iii claims decided markets are *"the one maker trade where the usual
toxicity argument inverts"* — stragglers dump at 95-97c into thin books, so a
resting bid earns the impatience premium with minimal adverse selection.

Maker markout by entry-bid band (conservative fill: filled only when a trade
printed strictly through the resting price; Kalshi maker fee at 25% of taker):

| entry bid band | n fills | markout | 95% CI |
|---|---|---|---|
| [0.00,0.50) | 61489 | -0.0028 | [-0.0040,-0.0017] |
| [0.50,0.80) | 29296 | -0.0353 | [-0.0372,-0.0333] |
| [0.80,0.90) | 6688 | -0.0436 | [-0.0473,-0.0399] |
| [0.90,0.95) | 3505 | -0.0347 | [-0.0389,-0.0305] |
| **[0.95,0.99)** | **2521** | **-0.0295** | **[-0.0336,-0.0255]** |
| **[0.99,1.00)** | **380** | **-0.0187** | **[-0.0248,-0.0126]** |

**Not supported.** Adverse selection does not invert; the certainty band is
*worse* than the low band. H2 targets a 1-2c gross premium against a measured
~3c drag.

CAVEAT, stated because it is load-bearing: this is L7's CONSERVATIVE fill rule,
which is adversely selected by construction — you are filled precisely when
someone pushes through you. It is the pessimistic end of a bracket whose
optimistic (fill-at-touch) end is not built. H2 is therefore not dead outright;
it requires the true fill to sit far closer to the optimistic end, and the gap
to close is ~3c against a 1-2c target.

## 3. Probe — H7: are the tails rich enough to SELL after fees?

Sell YES at the bid, T-24h, two-sided books only, taker fee charged:

| yes-price band | n | clusters | P(yes) | sell edge | 95% CI |
|---|---|---|---|---|---|
| **[0.00,0.10)** | 7270 | 2341 | 0.0312 | **-0.0063** | [-0.0141,+0.0000] |
| [0.10,0.30) | 4126 | 2363 | 0.2380 | -0.0551 | [-0.0726,-0.0382] |
| [0.30,0.50) | 4847 | 3421 | 0.4436 | -0.0614 | [-0.0780,-0.0447] |
| [0.50,0.70) | 3221 | 2382 | 0.5731 | -0.0073 | [-0.0269,+0.0128] |
| [0.70,0.90) | 1982 | 1195 | 0.7326 | +0.0429 | [+0.0190,+0.0691] |
| [0.90,1.00) | 2019 | 753 | 0.9520 | +0.0091 | [-0.0042,+0.0265] |

**H7 as specified is not supported**: the tail band it targets is -0.0063.

The [0.70,0.90) cell looked like a +4.3c edge — one of six cells searched,
family-wise error ~26%, and the exact mirror of L8's worst buy-side band.
Rule 12's temporal split (discover early, confirm late; split at 2026-06-30):

| band | half | n | clusters | edge | 95% CI | |
|---|---|---|---|---|---|---|
| [0.70,0.90) | EARLY | 1000 | 595 | +0.0681 | [+0.0332,+0.1059] | discovers |
| [0.70,0.90) | LATE | 982 | 600 | +0.0172 | [-0.0159,+0.0528] | **fails to confirm** |
| [0.00,0.10) | EARLY | 3992 | 1273 | -0.0037 | [-0.0145,+0.0039] | |
| [0.00,0.10) | LATE | 3278 | 1082 | -0.0095 | [-0.0223,-0.0002] | negative both halves |

**Winner's curse, caught.** Without the split this would have been reported as
a 4.3c edge. It is concentrated in the early half and does not replicate.

## 4. What would unblock the H-series

Every hybrid is gated on the C-series, and the C-series queue says three of
them are retrospective and immediate on data we already hold: C3
(implied-vs-realized econ ladders), C9 arms i-ii (certainty discount series),
C12/C13 (cluster dispersion, information cartography — 1-minute candles,
keyless). C1 needs ~a season of weather dailies. B0 needs one PDF read.

Cheapest path to the first runnable hybrid: pin B0, run C3, and H7 has both
parents — though probe 3 above says its mechanism is already in doubt.
