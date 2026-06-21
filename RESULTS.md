# RESULTS — what the measurement harness found

*A paper-trading study of cross-venue prediction-market pricing. Every number below is
recomputed from an append-only SQLite ledger; methodology was frozen in
[PREREGISTRATION.md](PREREGISTRATION.md) before its data accumulated.*

## Question

Do the pricing inefficiencies that are visible across Kalshi, Polymarket, and SX Bet —
cross-venue arbitrage, and +EV vs. sharp sportsbook consensus — survive contact with
reality (simultaneity, executable depth, and settlement)?

## Data

- **~945,000 quotes** persisted across **~1,140 polls** (every quote of every sweep, kept
  forever as backtest fuel).
- **3,100+ paper bets** in `flat_bets`, append-only, each carrying optimistic (first-sighting)
  and confirmed (second-sighting) fill prices plus a verdict trail.
- Venues: Kalshi, Polymarket, SX Bet (public, unauthenticated reads only — no orders ever
  placed). Sportsbook consensus via The Odds API.

## Method (one paragraph)

Prices are converted to one unit — effective cost per $1 of payout (real order-book ask +
venue fees). Detection runs per game × market × exact line. Every flag is a $1 paper bet,
then graded down an evidence ladder: depth floor → match-suspect quarantine → time-skew
label → flash re-quote (VERIFIED/VANISHED) → settlement. Cross-venue flags stay REVIEW
until both rulebooks are read. Flat-$1 is the honest headline; quarter-Kelly is a parallel
projection. Full method and de-vig in PREREGISTRATION.md and EV_SPEC.md.

## Finding 1 — cross-venue sports arb is a mirage on confirmed fills

The headline result, and the project's reason for existing. Of arbitrage flags taken to
settlement, the locked-at-entry P&L over **44 settled arbs totals +$0.13** — about
**$0.003 per bet**, statistically indistinguishable from zero. The flash verifier explains
why before settlement even arrives:

| Flash grade | Count | Meaning |
|---|---|---|
| VERIFIED | 33 | edge survived a re-quote seconds later |
| VANISHED | 10 | edge gone on re-quote (stale quote) |
| UNVERIFIABLE | 32 | an SX leg that (until fixed) couldn't be re-priced |

Even the VERIFIED arbs are sub-1% and capped by thin book depth; the realized edge after
fees, slippage, and the settlement-rule risk that no re-quote can see (extra innings,
postponement voids, divergent oracles — SPEC §12) collapses to ≈$0. **This is the central,
defensible result: riskless cross-venue capture in these markets is, for a retail paper
trader, a mirage.** The instrument was built to discover that honestly, and it did.

## Finding 2 — the +EV signal is contaminated, and most of it isn't real

856 EV flags were recorded. The raw edge buckets were incoherent — a ">5%" bucket showed
four-digit average "edges". Diagnosis (Phase 1): those are tiny asks ($0.01–0.02) against
empty/one-sided books, exactly the SPEC §12 traps. The quality layer labels them
(`suspect-edge`, `thin-book`) without deleting — **8 suspect-edge contaminants quarantined**
— and the report shows a clean column beside the raw one. On the clean signal, the CLV test
(does a flag beat the venue's own close?) has so far **not** cleared its pre-registered bar
(mean CLV ≤ 0, CI includes 0 at n below threshold), so EV betting **stays disabled**. The
system refuses to enable betting on an unproven signal — by design.

## Finding 3 — where edge might actually live (built, measuring)

The cross-venue mirage motivated three lower-risk strategies, each pre-registered:

- **S1 negative-risk basket arb** (intra-Polymarket, *no* cross-venue settlement risk): the
  single cleanest construct. Live sweep over 32 negRisk events found 0 arbs — Polymarket
  baskets are efficiently priced (Σ YES ≈ 1.008 on a recorded example). Honest null so far;
  the lifetime distribution of any future flag is itself the result.
- **S2 favorite-longshot bias**: calibration curve over settled clean bets; awaiting
  ≥200 settled bets per band before any band is actionable.
- **Lead-lag** (archive analysis): Kalshi appears to lead Polymarket (~42s median vs ~209s
  reverse), but the result is confounded by Polymarket's 9× higher move density and is not
  trusted until null-corrected.

## Limitations (stated plainly — honesty is the point)

- **Paper fills.** Optimistic−confirmed is a slippage *proxy*, not real execution; latency
  and partial fills are simulated, capital fragmentation excluded.
- **Sample size.** Several hypotheses are below their pre-registered n; those readings are
  plumbing checks, not verdicts.
- **De-vig bias.** Proportional de-vigging mildly biases favorites/longshots; per-book
  probabilities are stored so flags can be re-graded under another method without re-running.
- **One settlement source.** Settlement is score-based with venue-resolution reconciliation
  specced but not yet primary.

## Reproduce

```bash
pip install -r requirements.txt
pytest                                  # 100 tests
python -m arb report                    # ledger totals
python -m arb scores                    # per-segment edge, blocked vs eligible
python -m arb backtest-flb              # favorite-longshot calibration
python -m arb dashboard && open reports/dashboard.html
```

The pre-registration, the honest mirage result, and the append-only ledger are the three
things that survive scrutiny.
