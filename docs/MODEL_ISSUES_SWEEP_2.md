# Second sweep — sizing model, EV-exit ledger, scorer, dual-track "clean" (June 2026)

A fresh pass focused on modules the first sweep didn't reach: `arb_sizing.py` (the depth/edge
sizing model behind your "bet more when more depth and edge" question), `experimental/ev_exit.py`
(take-profit/stop ledger), `scoring.py` (segment gate), and the EV detection tail in `cli.py`.
Same tags as `MODEL_ISSUES_SWEEP.md`: **[ERROR] / [SLOW] / [LEAK] / [ARGUABLE]**. Numbers are
from the live `data/arb.sqlite`.

The theme this pass: several "fixes" are wired but **inert or self-defeating on the current
data** — they look active in code but do nothing (or the wrong thing) given what's actually in
the ledger.

---

## The sizing model you asked about is inert by construction

### G1 [LEAK] — `arb_sizing` trains on an EMPTY population, so it can only ever recommend flat $1

`build_arb_sizing_model` / `build_arb_scale_projection` / `recommend_arb_stake` all train on:

```sql
WHERE kind='ARB' AND status='SETTLED' AND trusted=1 AND stake_usd>0 AND pnl_optimistic IS NOT NULL
```

Live count of that population: **0**. (314 settled arbs, *all* `trusted=0`; only 2 distinct
trusted fingerprints exist in any status, none settled.) So every bucket has `n=0 < min_sample`,
`recommend_arb_stake` hits the "too few settled staked ARBs" reasons and returns
`stake=$1.00, scalable=False` every time.

This is the direct answer to your earlier question ("how aggressive can I be… bet more when more
depth and edge"): **the model that would answer it cannot, today** — not because the logic is
wrong but because there are no trusted, settled, staked arbs to learn from (`trusted_pairs: []`).
It will stay inert until you whitelist a venue-pair *and* let it accumulate settled trusted arbs.

**Fix/decision:** this is the same root as ARB L1. Until a pair is whitelisted and reconciled
(ARB A2), depth/edge sizing is undefined; the honest output is "flat $1, insufficient data,"
which is what it returns — but nothing surfaces *why*, so it looks like a working model
outputting a conservative answer. Make the empty-population state explicit in the report.

### G2 [ERROR] — hardcoded magic numbers violate hard-rule #4

`arb_sizing.py` hardcodes `EDGE_CAPS = {0.5-1%:$5, 1-2%:$15, 2-5%:$50, 5%+:$100}`,
`market_depth_fraction = 0.01`, and the `DEPTH_BUCKETS`/`EDGE_BUCKETS` bounds — none in
`config.yaml`. CLAUDE.md rule #4 says every threshold/cap lives in config. These caps directly
size money, so they're exactly the kind of constant the rule targets.

**Fix:** move the caps, the depth fraction, and the bucket edges into `config.yaml` under an
`arb_sizing:` block.

### G3 [ERROR] — sizing trains/gates on `pnl_optimistic`, not reconciled realized

Despite the careful "trusted only" docstring, the ROI used for the gates
(`stats['roi'] < 0.01` etc.) is `pnl_optimistic` (`arb_sizing.py:139,191`). Since reconciliation
is a structural no-op (ARB A2), "realized" never diverges from the optimistic lock, so when a
pair *is* eventually whitelisted the model will scale stakes on optimistic ROI that has never
been validated against venue-actual settlement. **Fix:** train on the reconciled realized P&L
once ARB A2's resolvers exist; until then, cap scaling and label it provisional.

### G4 [ERROR] — depth scaling uses unnormalized single-level top-of-book

`binding_depth_usd` is `min(leg.depth_usd)` over legs, and the recommended stake is
`binding_depth × 0.01`. But `depth_usd` is the single-level, cross-venue-incomparable top-of-book
quantity (ARB B3/B4: Kalshi units undocumented, Polymarket is whole-event liquidity in fallback,
SX is a derived taker notional). So even when G1 is resolved, the "depth" the model scales on is
the same fantasy number the capacity report uses. **Fix:** depends on ARB B1/B3 (real ladders +
normalized depth) landing first.

---

## EV-exit ledger corrupts the optimistic/confirmed split

### H1 [ERROR] — `_append_exit_row` books the SAME number into both P&L columns

The whole ledger's integrity rests on `pnl_optimistic` vs `pnl_confirmed` being *different* (the
"mirage" gap). But the take-profit/stop writer sets **both** to `decision['profit']`
(`ev_exit.py:178`), and `profit` is computed from `entry_cost = confirmed_entry_cost OR
optimistic_cost` (`ev_exit.py:103`). So when a bet never confirmed, the optimistic entry cost is
booked as **confirmed** P&L — overstating the confirmed (realistic) curve precisely for the bets
that were never capturable. EXITED rows are terminal and counted by the daily report's settled
P&L, so this leaks an optimistic number into the confirmed headline.

**Fix:** compute two profits — optimistic from `optimistic_cost`, confirmed from
`confirmed_entry_cost` (NULL → confirmed P&L NULL, not the optimistic value) — and write them to
their own columns, mirroring `settlement._append_settled_row`.

### H2 [ERROR-risk] — EV-exit hits Kalshi with no rate limiting

`_kalshi_current_bid` calls `requests.get(.../orderbook)` directly (`ev_exit.py:193`), bypassing
the adapter's `MIN_REQUEST_INTERVAL_SECS=0.15` + 429 retry. One live call **per open EV candidate
per cycle**; with many opens and a frequent `ev-exit` daemon this can trip Kalshi's 429 and (no
retry) silently return errors, so take-profits are missed → positions ride to settlement with
more variance. **Fix:** route exit reads through the rate-limited adapter session (or add the
same throttle/retry here).

### H3 [ERROR/LEAK] — exit profit is measured fee-loaded on both ends

Take-profit ROI is `exit_bid/entry_cost − 1` where `exit_bid` is net of the **sell** taker fee
and `entry_cost` includes the **buy** fee (`ev_exit.py:110`). Charging both ends biases exit
profit downward (same flaw CLV has, EV_WEAKNESSES #9), so the `min_profit_roi` gate fires less
often and profitable exits are left on the table. *Argument it's fine:* conservative is safe for
paper. *Argument it's a leak:* it systematically suppresses a real, capturable action (closing a
winning position). **Verdict:** measure exit P&L against the no-fee mid for the *decision*, keep
fees in the booked P&L — separate the signal from the accounting.

---

## Scorer is dominated by zeros, which silently blocks segments

### I1 [ARGUABLE→ERROR] — 42% of clean settled EV is scored as exactly $0 P&L

`scoring._settled_clean_bets` scores on `pnl_confirmed`, treating NULL as `0.0`
(`scoring.py:110-112`). Live: **256 of 613** clean settled EV bets have `pnl_confirmed IS NULL`
(42%). So nearly half the sample is hard-zeroed, dragging every EV segment's ROI toward 0 and,
through the segment gate (`block_below`), potentially **BLOCKING segments that are profitable on
the optimistic fill**.

*Argument it's correct:* a bet that never reached CONFIRMED was never capturable, so 0 P&L is the
honest realized result; scoring on optimistic would reward a mirage.
*Argument it's an error:* a 42% non-confirmation rate is itself the finding — either confirmation
(second *consecutive* sighting) is too strict, or EV flags vanish between polls (a capture/latency
problem). Either way, the scorer's verdict is mostly "we couldn't capture it," not "the edge
isn't real," yet it's used as an edge gate. **Verdict:** keep confirmed-based scoring, but report
the confirmation rate per segment alongside the score, and investigate why 42% never confirm —
that rate is a bigger profit lever than the ROI number it produces.

### I2 [ERROR] — arbs can't pass the segment gate (RESOLVED + NULL confirmed → ROI 0)

Settled arbs have `result='RESOLVED'` and usually `pnl_confirmed IS NULL` (arbs realize at entry;
no CONFIRMED child cost), so `_confirmed()` returns 0 for them → arb segment ROI ≈ 0 → BLOCKED or
INSUFFICIENT. Combined with G1/L1, the arb classes are structurally unable to earn a passing
segment score. **Fix:** for arbs, score on the locked (optimistic==realized-at-entry) P&L, since
the arb's confirmed cost *is* the entry cost; the EV "confirmed" concept doesn't map onto a lock.

---

## Dual-track "clean" is inconsistent across kinds

### J1 [ERROR-risk] — EV is force-flagged `status=REVIEW` but still counts as clean

`detect_ev_from_consensus` sets `status="REVIEW"` + `event-match-review` on **every** EV
unconditionally (`cli.py:632`). Yet the clean/headline aggregates key on `quality_flag IS NULL`,
not status — so REVIEW EV still counts as clean, while REVIEW *arbs* are excluded via `trusted=0`.
"Clean" therefore means two different things by kind: for EV it's `quality_flag IS NULL`
(status ignored), for arbs it's `trusted=1`. Anyone reading `status` to mean "actionable" is
wrong for EV. **Fix:** pick one actionable predicate and apply it uniformly; if EV's REVIEW is
truly decorative, stop setting it (it dilutes the signal for the cases where REVIEW means
something).

### J2 [LEAK, latent] — the EV uncertainty/decay fixes are wired but OFF by default

`ev_edge_decay_per_hour: 0` (EV#7 staleness decay off → full edge counts to the binary cutoff)
and `ev_dispersion_gate: false` (EV#3 edge/SE gate + dispersion Kelly haircut off). Both are
implemented in `detect_ev_from_consensus` but disabled in config, so the model still over-counts
stale and high-dispersion edges. *Argument:* they're parallel/measurement until validated.
*Verdict:* fine to keep off, but the headline EV currently includes the very biases those knobs
exist to remove — say so in the report, and prioritize validating + enabling them.

### J3 [ARGUABLE] — FLB calibration is fit on MLB only, applied to all sports' EV

The `apply_calibration` path fits a favorite-longshot curve from `population_binaries(store)`,
which returns `[]` for any sport but `baseball_mlb` (`quote_settlement.py:80`). The resulting
calibrator is then applied to EV in *every* consensus sport (currently MLB + NBA). NBA EV gets an
MLB-fit correction.

*Argument it's fine:* favorite-longshot bias is broadly similar across team sports; an MLB curve
beats no correction. *Argument it's an error:* it's still a cross-sport extrapolation applied as
if measured, and NBA scoring/totals distributions differ. **Verdict:** acceptable as a stopgap if
labeled, but the calibrator should be per-sport (or only applied to the sport it was fit on);
right now nothing flags that NBA is getting MLB's curve.

### I3 [SLOW] — `population_binaries` scans all historical WINNER quotes each call

`SELECT … FROM quotes WHERE market_type='WINNER' AND live=0 AND sport=?` has no time bound and no
composite index on `(sport, market_type, live)` — against 2.0M quotes this is a wide scan, and it
runs whenever `flb.apply_calibration` is on (per scan). **Fix:** bound to a recent window and add
the composite index, or cache the fitted calibrator across polls.

---

## Priorities (this sweep)

1. **H1** — EV-exit double-books optimistic as confirmed; corrupts the confirmed headline. Small,
   contained fix.
2. **I1/I2** — the scorer's zero-drag and arb-can't-score behavior decide which segments are
   allowed to bet; understand/relabel before trusting any segment gate.
3. **J1** — unify the "actionable/clean" predicate across EV and arbs; the current dual track is
   a foot-gun.
4. **G1/G2/G3/G4** — the depth/edge sizing model: surface its empty-population state, move the
   magic numbers to config, and don't trust it until ARB A2 (reconcile) + B1/B3 (ladders) land.
5. **H2/I3** — rate-limit EV-exit's Kalshi reads; bound the calibration query. Operational.
6. **J2/J3** — validate and enable the EV decay/dispersion knobs; make FLB calibration per-sport.
```
