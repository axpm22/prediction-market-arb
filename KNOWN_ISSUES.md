# Known issues and failures — 2026-07-29

Everything found or confirmed during the 2026-07-28/29 measurement session, ranked
worst first. "Worst" = most likely to make the system report a number that is
wrong, lose money, or block all progress.

Each entry states what is *measured* versus what is *inferred*. Nothing here is
an estimate unless it says so.

**Status 2026-07-30:** six code-level defects closed (#1, #3, #5, #6, #8, #28) —
see the `*FIXED*` line on each. None of the fixes made the system more
permissive: #1 and #3 make the §18.2 gate computable and connected while the
rule-of-three arithmetic still refuses every position at n=43; #5, #6 and #28
only withhold trust; #8 is additive. Verified after all six: **banksim clean-lane
P&L is still $0.00** and clean-lane positions in the ledger are still 2.

---

## Tier 1 — Blocks everything, or would lose money

### 1. `certificate_n` is read but never written — the cross-venue path is unimplemented
`arb/eligibility.py:77` reads `o.get("certificate_n")` and returns
`"no-certificate"` when it is 0. **Nothing in the codebase ever sets it.** Every
`XSPREAD` is therefore review-lane forever, regardless of config. The Resolution
Equivalence Certificate the SPEC specifies (n mismatch-free reconciled positions
per venue pair) has no writer.
*Consequence:* there is no configuration that promotes a cross-venue position.
The feature was specified and not built.

*FIXED 2026-07-30 (57fbe52):* `reconcile.certificate_n` derives it from unioned reconcile verdicts; `tracker` pins it like `pair_demoted`. **The arithmetic still gates — nothing promotes at n=43** (charge 6.98pts vs a 0.5pt median edge → `net-spread-nonpositive`).

### 2. Settlement-risk arithmetic exceeds every observed edge
`net = gross − (3/n)·LGM`. With n=41 mismatch-free pairs and LGM=1.0 the charge is
**7.32 points** against a **0.5-point** median gross edge → net **−6.82 points**.
Break-even needs **n ≥ 600**.
*Consequence:* even with #1 built, cross-venue is deeply negative-EV today. This
is arithmetic, not policy — overriding it requires falsifying LGM or n.

### 3. The settlement matrix is never consulted — the §18.2 gate is disconnected
`arb/eligibility.py:87` gates trust on `cfg["trusted_pairs"]` directly.
`settlement_matrix.sport_all_green()` has **zero callers**. Verified rulebooks
cannot promote anything.
*Consequence:* the work of verifying settlement rules currently has no path to
affecting behaviour. Left in place deliberately (not parked in `attic/`) because
it is a safety gate, not dead weight.

*FIXED 2026-07-30 (f449971):* `classify` now calls `sport_all_green`, ordered after `pair_demoted` so an observed mismatch outranks an unverified rulebook. **Still gates — every shipped matrix cell is AMBER, so no sport promotes**, and tennis cannot even when whitelisted (`tennis.walkover` is RED).

### 4. Two settlement divergences confirmed from primary sources
- **Tennis retirement:** Pinnacle voids and refunds; Kalshi pays the advancing
  player "after a ball has been played"; Polymarket pays "the player who
  advances". Book-vs-venue mismatch.
- **Walkovers:** Kalshi resolves to "a fair price in accordance with the rules";
  Polymarket resolves to a hard **50-50**. **Venue-vs-venue**, on a pair a human
  had already adjudicated as exact complements.
*Consequence:* rule 6's stated risk is real, measured, and present in a mapped
pair. `tennis.walkover` set to RED.

### 5. SX is structurally unusable and still in the pipeline
- **0 of 356,595** SX quotes carry a bid → SX legs can never be unwound or
  marked.
- SX settled-status semantics unverified (`data/verified_facts.md`), so
  `reconcile._sx` returns `None` unconditionally — SX legs never grade.
- Chain deprecation "coming months" (venue death, taxonomy class 10).
- SX legs quoted at **0.001–0.041** produced the largest "edges" in the ledger
  (15%–98%), all fake.
*Consequence:* both clean-lane positions in the entire ledger are SX. Every
top-edge artifact is SX. It should be excluded from headline stats or removed.

*FIXED 2026-07-30 (c10dad1):* `clean_lane_excluded_venues: [sx]` blocks SX on either leg, for every kind including `sx:self`. Did NOT retro-edit the 2 historical SX clean rows (rule 9 — append-only).

---

## Tier 2 — Invalidates measurements

### 6. The zombie guard catches 0.76% of dead books
`_zombie` requires `volume_24h == 0` **AND** `spread ≥ 0.90`. The median
zero-volume quote has a **2¢** spread, so **99.2% of dead books pass detection**.
*Partially mitigated* by the new `max_lead_hours` demotion, which is a proxy, not
a fix. The guard itself is still too narrow.

*FIXED 2026-07-30 (08660dd):* a spread-vs-edge guard demotes when the half-spreads we must cross exceed the edge, with `spread-unknown` reported separately from `spread>edge` so a missing bid is not recoded as a measured cost. The zombie guard itself is unchanged and still narrow.

### 7. L5's 27.9s lag is probably a sampling artifact
We poll at 60s, so timestamp resolution *is* 60s. A median lead of
**27.9s ≈ 60/2** is what a mixture of "same cycle" (0s) and "next cycle" (60s)
produces. Measured intra-cycle fetch skew is only +0.28s, so the confound is the
grid, not the fetch order.
*Consequence:* the **only favourable directional signal the instrument has
produced** may be measuring its own sampling. Unresolved. Requires burst polling
at ≤4s to settle (feasible: 2 requests gets both venues' full MLB board, so 1
snapshot/second fits inside the proven-safe 2 req/s).

### 8. Depth is unmeasurable from the ledger
`flat_bets.legs_json` stores `depth_usd: None`. 74% of positions in the 30-day
backtest used **imputed** depth (median $56). Kalshi stores **no ladder at all**
(0 of 3.9M quotes), so walk-the-book capacity cannot be computed for Kalshi legs.
*Consequence:* every capacity and $/day figure rests partly on an assumption.
An early "all legs have zero depth" conclusion was itself an artifact of `or 0`
turning `None` into 0.

*FIXED 2026-07-30 (45e3e05):* `tracker` persists `depth_usd` per leg. Consequence worth noting: the first position with real depth showed $982/$623 against the imputed $56 median, so earlier all-lanes $/day figures were UNDERSTATED for depth reasons while being overstated for artifact reasons. Kalshi ladders are still absent.

### 9. Non-position lanes were judged on a money scale
`null_band` is denominated in money-per-dollar, but L1's primary is a prevalence,
L2's a rate, L5's a probability excess, L9's overround points. Fixed by
per-lane `null_band`, but the same class of error is easy to reintroduce.

### 10. In-play data is the worst in the system
Live-lane XSPREADs show a **14.92¢ average edge** and 12/12 covering sums below
1.0 — because legs are quoted at **0.001** on suspended books. Fake-arb class 8
(suspended-but-quoted). Genuine in-play half-life is ~3.6s against our 20s live
cadence, so what survives to be seen is by construction not real.

### 11. Collection coverage was 2–83% and initially scored as market quiet
Per-3-day-block watch coverage: 2%, 17%, 20%, 39%, 51%, 51%, 83%. Blocks with
2% and 17% coverage were reported as "$0.00/day" as though the market were quiet.
Rule 15's coverage mask exists precisely for this and was not applied until
prompted.

### 12. Winner's curse demonstrated live
A `[0.70,0.90)` sell-side band read **+0.0429** pooled and would have been
reported as a 4.3¢ edge. Temporal split: **+0.0681 early, +0.0172 [−0.0159,
+0.0528] late — fails to confirm.** Rule 12's split is load-bearing, not
ceremony.

---

## Tier 3 — Real defects, fixed this session

### 13. gamma `closed=true` — every Polymarket settlement verdict was unresolvable
`GET /markets?condition_ids=` defaults to `closed=false`, filtering out exactly
the resolved markets the resolver reads. **Every PM leg returned None since the
lane began.** 49 kalshi:polymarket pairs had their Kalshi leg graded and their PM
leg stuck forever. Fixed ([9b67b10]); 41 pairs now audited, 0 mismatches.
*This is the single highest-impact fix of the session — before it, L2's n was
permanently 0 and could never grow.*

### 14. Far-from-start flags were the majority of the ledger
89 of 125 locked flags were raised >6h pre-event, on books with median 24h volume
of **3** versus **211,365** within 6h; half had zero volume. Fixed by
`max_lead_hours: 12` demotion (REVIEW, never dropped — rule 7).

### 15. Reconcile verdicts were read per-event instead of unioned
`reconcile` grades one leg per pass, so a fully graded pair appears as
`["WON", null]` then `[null, "LOST"]`. Reading events independently reported it
as two half-graded pairs. Fixed.

### 16. Banksim counted the quarantine as tradeable
All-lane $/day was reported as income when every position was review-lane. Fixed
by a `clean-lane` column: **$0.00 in all 49 snapshots of the 24h run.**

### 17. Verdict ladder tested SIGNAL before NULL
An effect could exclude zero and be economically negligible yet rank as a live
finding (L11: −0.0038 with CI inside ±1¢). NULL now tested first — can only
demote, never manufacture, a positive.

### 18. L8 was mislabelled "calibration"
It prices at the ask, so it bundles calibration error + half-spread + fee. Every
band came back ≤0 with favourites worst, which reads as favourite-longshot bias
and is actually the cost of taking. Renamed to "after-fee TAKER edge".

---

## Tier 4 — Faults I introduced

### 19. Global rate limiter caused a ~2-hour outage of the primary lane
`_pace_global` held its flock across the sleep; PM fans 9 threads per cycle; the
2→9 sport expansion pushed cycles past their 60s interval; cycles overlapped and
contended with themselves. **Congestion collapse — watch wrote 0 quotes for ~2h.**
Diagnostic tell: raising the deadline 30s→150s moved the failure from 32s to 151s
(failing slower, not succeeding). Reverted ([8367560]).
*Two aggravating factors:* all 360 tests passed throughout, because conftest
disables pacing (rule 2 — no live venue calls), so the bug lived where the suite
cannot look. And a post-deploy smoke check would have passed: it worked at 19:07
and degraded by 19:52.
*Root misdiagnosis:* demand was ~1.6 req/s, never near any limit. I built a
solution to a problem the data did not show.

### 20. Diagnosed a "spin loop" that did not exist
`datetime('now','-20 minutes')` yields `YYYY-MM-DD HH:MM:SS` but `started_at` is
ISO with a `T`, and `'T' > ' '`, so the comparison matched everything today.
Reported 6.71 req/s and 1,274 watch polls/20min; the real figures were 0.10/s and
20. Caught before acting.

### 21. Nearly implemented a fix for a non-bug
Concluded from `attic/`'s own note that the Kalshi `mutually_exclusive` flag was
unfetched, and stated it as "one missing fetch" with "a real payoff". Wrong: the
flag is wired on both venues (`kalshi_events.py:105`), the API supplies it (19 of
50 events true), and the 26 failing baskets are genuinely `me=false` **threshold
ladders** ("how high will unemployment get") where outcomes are nested cutoffs.
The guard was correct. Verified before building.

---

## Tier 5 — Structural findings that are not defects

### 22. Cross-venue arbitrage does not clear transaction costs
3,458 cross-venue covering observations: median **1.0242**, only **0.29%** below
1.0, and those mostly stale. The venues agree to within the fee+spread barrier
(~2.4¢) 99.7% of the time. Not suppressed by gates — not there.

### 23. Opportunity supply, not capital, is the binding constraint
24h banksim, 49 snapshots: **zero** missed-for-capital at $250/$500/$1,000, with
utilisation 31–40%. Every bankroll projection made earlier answered a
non-binding constraint.

### 24. Coverage is the only validated lever, and Kalshi is the narrow side
Whole history: 1,977 Polymarket events, **264 Kalshi**, intersection **156**.
Kalshi constrains by 7.5×, purely because 4 of ~240 series were scanned. The
2→9 sport expansion (landed outside this session) roughly doubled cross-listing
to 113–117 concurrent.

### 25. Same-venue is the only construct that reaches the clean lane
2 clean positions in the entire ledger (avg edge 1.19¢), both same-venue ARB on
SX. Kalshi ME baskets are correctly refused (26/26 `me=false`); PM negRisk
baskets have the flag but fail the other proof conditions (204 opps, 0 proven) —
**the one genuinely open lead**, since those failures are tolerance-based rather
than structural.

---

## Tier 6 — Operational

### 26. Supervisor warns about lane crowding and nothing enforces it
"lane watch phase separation 10.0s < stagger_secs 15.0 — cadence too fast for 6
lanes on one IP". Advisory only. The attempt to enforce it is #19.

### 26b. L14 deferred non-sports roots — weather, economy/business/science, esports
*Registered 2026-09-04 with the non-sports roots amendment.* The whale lane now
scores and books `politics`, `geopolitics` and `crypto` beside `sports`. Four
candidate roots were deliberately left out, each for its own reason, and each is
one `whale.nonsports.tags` line away from being added — but adding one is a
dated PREREGISTRATION amendment, because a new root is a new population with its
own n against the same kill rules.

- **`weather`** — operator decision 2026-09-04. It is the second-largest surface
  by market count (405 markets ≥ $1k/day, measured 2026-09-04) but the smallest
  by dollars ($1.7M/day across the whole tag, against politics' $20.7M), so the
  markets are individually tiny and a "whale" there is a few thousand dollars.
  It is also the tag whose markets Gamma answers a `gameStartTime` for, which is
  exactly the confusion the `markets.sport IS NOT NULL` discovery guard exists
  to prevent.
- **`economy` / `business`** — the economy surface is FOMC/CPI scheduled-release
  markets: an "insider" there is a rumour, and the efficiency is high; business
  has only 45 markets over the volume floor. Both are too thin or too efficient
  to reach n in a reasonable time.
- **`science`** — 9 markets over the floor. Not a population.
- **esports** — no `esports` hierarchy root exists (controller decision
  2026-09-04, see the `slug_sport_prefixes` note in config.yaml): the four
  observed prefixes stay unmapped rather than pooling CS2 skill into the sports
  root. This one needs a root, not a tag line.

A fifth candidate, `tech-and-science`, is not a tag slug at all — the probe
returned an empty list (data/verified_facts.md 2026-09-04).

### 27. Interpreter changed
The framework Python the supervisor ran under since 17 July no longer has
`dotenv`; it now runs under `.venv/bin/python`. Children inherit via
`sys.executable`.

### 28. One market re-flagged 234 times in 45 minutes
`nonsports:map:kxcpicore-26jul-t0.0`, the only OK-status opportunity in the
system. Its edge does move (111 distinct values), but the underlying Kalshi book
is **11¢ wide** — a 1.5¢ "arb" requires crossing it. Not booked (the tracker's
reappearance cap holds), but it dominates the opportunity table.

*FIXED 2026-07-30 (08660dd, completed in a follow-up):* the spread-vs-edge guard demotes it — a 1.5c edge inside an 11c book is `spread>edge`. **08660dd alone did NOT close this**: the mapped lane builds its own `Opportunity` instead of routing through `arbitrage._locked_opp`, so it inherited none of that function's guards and the CPI pair was still emitted OK afterwards. Caught by checking production output rather than trusting the commit.

### 29. Storage grows linearly with cadence × coverage
22.5M quotes to date; rule 7 forbids not persisting. Tier-1 speed work would
reach 250M+, which needs a ladder-retention decision *before* switching on.

### 30. `db_backup.dest` is on the same volume as the DB
Warned on every startup. A disk failure loses both.

### 31. Config with no implementation, removed 2026-09-03
`tape_check` and top-level `exit` (config.yaml, `arb/config.py` SCHEMA) had no
reader anywhere under `arb/` or `tests/` — grepped and confirmed before
deletion. Their intent is carried forward: `tape_check`'s "confirm a fill only
if the venue printed a trade at-or-through price, in-window" becomes the whale
print collector (L14 task 3, the same tape-confirmation idea against the
whale-consensus lane's own tape). `exit`'s SELL-engine fields (capture/trail
fractions, promote_requires) become `whale.exit` (signal-driven exits — flip
ratio, sold fraction — design doc 2026-09-03 spec §9), a narrower mechanism
scoped to the new lane rather than a general read-time engine nothing called.

### 32. Polymarket data-API terms of use — OPEN operator decision (spec §3)
The L14 whale-consensus lane's print collector, backfill, and resolution
reader (`arb/whale_tape.py`, `arb/whale_backfill.py`, `arb/whale_resolve.py`)
read `data-api.polymarket.com` and `gamma-api.polymarket.com` automatically,
on a timer. Those reads are unauthenticated and reachable — confirmed live,
`data/verified_facts.md` — but whether Polymarket's terms of service permit
this kind of automated, scheduled polling is unverified: the ToS page could
not be fetched during research (design doc §3, 2026-09-03). The collector
rate-limits itself (`whale.max_calls_per_min`, default 30) and backs off
(`whale.backoff_secs`, default 300) on any non-200 response, so it degrades
rather than hammering the endpoint if access is ever restricted or contested,
but that is a courtesy to the venue, not a resolution of the ToS question.
The operator must decide before `supervisor.processes.whale` runs live under
the supervisor. No decision is recorded here — do not infer one from the
config default.

### 33. L14 day-one exit churn — the flip recount, closed 2026-09-04
The L14 lane's first supervised day fired 21 V1/V4 signals and 16 early
exits. **12 of the 14 `flip` exits were spurious**: they carry
`points_side` 0 and `points_total` 1–2 and fired 55–153 minutes after
booking. The cause was the recount reading the FRESH-PRINT tally
(`qualifying` over `signal_window_secs` 7,200 s) — the whales that formed
the signal still held every contract they had bought, but their prints had
aged out of that window while one opposing fresh print remained, so the
recomputed ratio was 0/1 and the position was closed at the bid for nothing.
Ageing out is not a change of mind, and no `left` exit fired on any of them
because nobody had sold.

A second, quieter half of the same defect: the collector ran
`takerOnly=true`, one row per fill, and a live probe that day showed maker
legs carry the MAKER's own `proxyWallet` and `side` — so a whale exiting
through a resting sell order produced no row the lane could see at all.

Fixed 2026-09-04 (PREREGISTRATION row of that date): the `flip` recount runs
over the whales still HOLDING under the bet's own variant; the collector
records both legs of every fill; and a second `left` test reads each signal
whale's current holding from data-api `positions?user=`. The 16 exits already
booked stand as recorded — `flat_bets`/`bet_events` are append-only (rule 9)
— and their hold-to-resolution counterfactual is reported as before, so the
cost of the churn is measured rather than erased.

### 34. L14 materialized-positions rebuild window — the lane scores off a partial cache while it fills
Opened 2026-09-05 with the per-wallet materialization (design §5 step 6
amendment of that date, `arb/whale_mat.py`). The 60 s scoring pass no longer
rebuilds every wallet's positions from every print; it reads a derived
`positions_mat`/`hygiene_mat` cache that the cycle brings up to date
`whale.mat.max_wallets_per_cycle` (400) wallets at a time.

**The cost, stated rather than hidden:** on the FIRST cycle after this ships —
and after any `whale_mat.MAT_VERSION` bump or `arb whale-mat --rebuild` — every
wallet on the tape is dirty, and the cache fills over several cycles. Until it
is full the lane scores, ranks and fires off a PARTIAL population: a wallet not
yet materialized has no positions, so it casts no vote and enters no elite
percentile. Signals are therefore *under*-counted, never over-counted, and no
number is wrong — it is computed over fewer wallets than the tape supports.

*Measured 2026-09-05* on a synthetic 800,000-print / 3,000-wallet tape
(scratchpad, never the production tape): a full first materialization is **8
cycles / 70 s of work at the shipped cap**, worst cycle 14.9 s, i.e. about 8
minutes at `whale.poll_secs` 60. The production tape held 763,786 prints and
~2,700 wallets on that date, so it is the same order.

The same window opens, smaller, whenever a `markets` row's `sport`,
`market_type` or `start_utc` actually MOVES: both the root filter and
`pregame` are functions of that row, so the tape's triggers dirty every wallet
that printed in the market. The 60 s cycle's own no-op rewrite of the universe
fires nothing (measured: 0.00 s for 1,000 markets), but a legacy re-map drain
at `whale.backfill.map_per_cycle` (1,000 markets a cycle) can dirty thousands
of wallets at once, and costs 46 s inside the whale-backfill child on that
same synthetic tape (400 prints/market; the production tape averages ~55).
Same consequence, same mitigation.

*Mitigation:* the backlog is on the ops line every cycle (`positions <rows>,
<n> dirty, <secs>s`, `arb report --daily` console line and the dashboard) and
in the `whale --once` output. A backlog that does not fall cycle to cycle is
the alarm; `arb whale-mat --rebuild` drains it in one unbounded run without
waiting for cycles. *Not fixed, by choice:* a bounded per-cycle rebuild is what
keeps the lane from stalling on its 300 s child cap, and blocking the lane for
a whole rebuild would be the worse failure.

---

## The two numbers that matter most

- **Tradeable P&L: $0.00/day.** Zero clean-lane positions in 49 consecutive
  snapshots over 24 hours. Not sampling noise.
- **n = 41 of 600** mismatch-free reconciled pairs needed before cross-venue
  arithmetic can clear. That is months of settlements, and #13 is what makes n
  able to grow at all.

Everything above is why the honest answer to "how much does it make" is zero, and
why the number is now trustworthy rather than merely low.

*Decision 2026-09-04 (operator):* proceed. The operator instructed "start the instrument, start everything" after the open question above; `supervisor.processes.whale` and `whale-backfill` were enabled in the Task 11 commit. Standing mitigations: the collector self-limits to `whale.max_calls_per_min` (30) and backs off `whale.backoff_secs` (300) on any non-200; all reads are unauthenticated public GETs; nothing is placed. If Polymarket signals otherwise (429s, challenge pages, a block), set `supervisor.processes.whale: false` and `whale-backfill: false` and restart.
