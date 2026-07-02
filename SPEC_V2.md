# SPEC_V2 — ArbModel V2 (paper-only cross-venue edge measurement harness)

> **Status: FROZEN.** This is the single source of truth. Every decision V1 got wrong
> silently is made here once, in writing, with a numeric vector. The coder (Sonnet 5)
> implements strictly from this file and the failing tests; it may not change a vector.
> Owner: Opus 4.8 (architecture + methodology + verification). Absorbs Fable 5's F4/F5/F7.
>
> Companion docs: `PREREGISTRATION_V2.md` (frozen hypotheses/gates), `SPEC.md` (V1 reference),
> `ArbModelV2_Plans/00–03` (context + data review), `Model - Sonnet 5.md` (coder contract).

---

## 0. Why V2 exists (the one-line diagnosis)

V1's headline bug was **architectural, not arithmetic**: safeguards (`REVIEW`, depth,
skew, timing) were set at *detection* and ignored by every path that *books money*.
Result: **314/314 settled arbs were forbidden REVIEW arbs** (A1). The organizing
principle of V2, absent from V1: **one eligibility predicate, defined once as a single
object, imported and called at every money-touching stage.** §3 is the heart of this spec;
everything else serves it.

Secondary lessons folded in: `p̂` is a distribution not a point (EV#3); confirmed-fill
totals are survivorship not slippage (R2); the two arb venue-pairs are different
populations (R3); SX priced at zero fee overstates half the arb book (R4/D3); apparent
edge is *inversely* related to win rate (R5); capacity numbers with no ladders are
fantasy (F6/B1).

---

## 1. Goals & non-goals

**Goal (unchanged from V1).** A paper-only, public-read-only, append-only measurement
harness answering honestly: *"if I'd bet \$1 on every flag, where would I be, and how much
edge survived contact with reality?"* Same shape as V1; structurally sound where V1 leaked.

**In scope.** Game/match **winner (moneyline)** and **totals (over/under, exact line)** on
Kalshi, Polymarket, SX Bet. Sports: MLB (only calibrated segment), plus World Cup soccer as
the second consensus key. Signal classes: **ARB, NEGRISK, DIVERGENCE, EV**. EV is
**measurement-only** (betting disabled until the pre-registered gate clears).

**Explicit non-goals.** No order placement, ever (paper only). No auto-fill of cross-venue
opportunities unless the venue-pair+market-type is whitelisted in `trusted_pairs`. No
weighted/Kelly ARB *headline* until real multi-level ladders exist (F6). No EV betting until
`PREREGISTRATION_V2.md` §3 CLV+calibration gate passes. No NFL. Non-sports: manual-map only.

---

## 2. Hard rules (non-negotiable, all phases)

1. **Paper only.** Public market-data reads only; no order/account endpoint, ever.
2. **Read-only feed credentials** are permitted *only* where §7 explicitly allows them
   (Polymarket WS keyless by default; Kalshi/SX signed feeds are opt-in and gated). The
   Odds API key is a query param. "No order endpoints" is absolute regardless.
3. **No Odds API credits in tests or loops.** Tests run on `tests/fixtures/`. Live calls
   only in `scan`/`settle`/`watch`. Log `x-requests-remaining` every live call; abort <50.
4. **No magic numbers.** Every threshold/fee/interval/decay constant reads from `config.yaml`.
5. **`Decimal` for all money math** (pricing, fees, stakes, slippage, ledger). Float is
   display-only; no float ever lands in a money column.
6. **`flat_bets` is append-only.** No `UPDATE`/`DELETE`; corrections are new annotation
   rows; every report recomputes from raw rows.
7. **Persist every quote** observed in a scan, opportunity or not.
8. **Fixtures-first** for every adapter. The SX maker-side inversion test must pass from a
   recorded fixture **before** the SX adapter touches the pipeline.
9. **Cross-venue is REVIEW-only** unless `venue_pair + market_type ∈ trusted_pairs`.
10. **The eligibility predicate is one object** (§3). Import and call it; never re-implement
    its logic in settle/reconcile/capacity/exposure/report. Re-implementation = VERIFY reject.
11. **SX quotes the maker's side:** taker cost `= 1 − percentageOdds/1e20`, and SX charges a
    **winning-bet fee** (R4/D3) — never model SX at zero.

---

## 3. The single eligibility predicate (F2) — the centerpiece

### 3.1 Definition (one function, one module: `arb/eligibility.py`)

```python
def eligibility(bet, cfg, now) -> Eligibility:
    """The ONLY authority on whether a flag may reach clean/headline P&L.
    Returns a frozen result object; never mutates the bet. Pure function."""
```

A bet is **eligible** (may enter the clean lane, settle into headline P&L, count in
capacity/exposure) iff **all** hold:

```
eligible(bet) =
    (bet.trusted == 1  OR  bet.venue_pair ∈ cfg.trusted_pairs[market_type])   # A1, rule 9
    AND  bet.start_utc is known (not start-unknown)                           # D1
    AND  bet.placed_at < bet.start_utc                                        # A3, C5
    AND  bet.depth_basis == "real"  (no event-level liquidity fallback)       # B3, D4
```

The predicate returns an **`Eligibility`** object, not a bare bool:

```
Eligibility(
    eligible: bool,               # the AND above
    reason: str,                  # first failing clause, or "ok"  ("review-untrusted",
                                  #   "start-unknown", "placed-after-start", "depth-unknown")
    lane: "clean" | "review" | "invalid",
)
```

- `placed_at >= start_utc` → `lane="invalid"` (the bet is retrospective; A3). **INVALID never
  settles into any lane** — not clean, not review.
- untrusted / depth-unknown / start-unknown but `placed_at < start` → `lane="review"`
  (displayed, tracked in the review lane, **never** in clean/headline P&L).
- all pass → `lane="clean"`.

### 3.2 Stages that MUST import and call this exact object

Every one of these calls `eligibility(...)` — no copy, no re-derived filter:

| Stage | Module | What it gates |
|---|---|---|
| detection | `arb/detection.py` | tag each opportunity's lane before display |
| fill / tracker | `arb/tracker.py` | which fills feed the clean-lane columns |
| settle | `arb/settlement.py` | only `lane=="clean"` bets settle into headline P&L |
| reconcile | `arb/reconcile.py` | provisional→final only for clean bets |
| capacity | `arb/capacity.py` | only clean, eligible arbs count toward capacity |
| exposure | `arb/exposure.py` | caps computed over clean lane |
| report (clean aggregates) | `arb/report.py` | the "clean" column of the honest-variant matrix |

### 3.3 Frozen tests for the predicate (Phase 6, highest priority)

- **T-ELIG-1:** fixture with one OK arb (trusted pair, start known, placed<start, real
  depth) + one REVIEW arb (untrusted pair) → only the OK arb produces a clean SETTLED row;
  the REVIEW arb appears only in the review lane. *(This is the exact test V1 failed.)*
- **T-ELIG-2:** `placed_at > start_utc` → `lane=="invalid"`, no settle in any lane.
- **T-ELIG-3:** `start-unknown` (Kalshi close_time fallback) → `lane=="review"`, reason
  `"start-unknown"`, never clean.
- **T-ELIG-4:** depth from event-level fallback → `lane=="review"`, reason `"depth-unknown"`.
- **T-ELIG-5 (architectural):** VERIFY greps every stage in the §3.2 table and asserts each
  imports `eligibility` from `arb.eligibility` (no local re-implementation). Enforced as a
  test: `grep -rn "def eligib\|trusted == 1\|trusted==1" arb/` finds the logic in exactly
  one file.

---

## 4. `position_key` and dedup (F3)

### 4.1 Definition

```
position_key = (sport, event_key, outcome, market_type, line)
```

One economic position, however many times re-flagged. V1 counted 1,280 arb rows as if they
were 1,280 positions; they were **149** (B2/C2). **Every aggregate dedups by `position_key`
before computing anything.**

`event_key = sport:date:sorted(norm(home),norm(away)):game_index` where `game_index`
disambiguates doubleheaders (D2, §9). `venue_pair` is a derived, sorted column on every
cross-venue row: `"kalshi+polymarket"`, `"polymarket+sx"`, `"kalshi+sx"` (R3).

### 4.2 The improved-quote rule

A re-flag of an existing `position_key` **links to the existing position** (updates its quote
history / best-seen price), it **never creates a new position** for aggregation. Distinct from
the *reappearance* rule (§8): a position that closed (absent ≥1 full poll) and later reappears
is a **new bet row** (`reappearance_of` link) but is still deduped to the same
`position_key` for economic aggregates.

- **T-POS-1:** 5 flag rows, 1 `position_key` → any aggregate reports `n_positions = 1`.
- **T-POS-2:** better price on re-flag updates `best_cost`, does not add a position.

---

## 5. Data model (SQLite, WAL) — schema deltas from V1

All money columns are **TEXT (Decimal serialized)** — never `REAL`. New/changed columns
relative to V1 (`SPEC.md` §6) are marked ⊕.

```
quotes(id, poll_id, scan_id, fetched_at, event_key, category, sport, venue, book,
       market_type, line, outcome, raw_price, cost, fee_cost, depth_usd, max_size_usd,
       live,
       ⊕ depth_basis TEXT,          -- 'real' | 'event-fallback' | 'unknown'  (B3/D4)
       ⊕ ladder_json TEXT)          -- captured multi-level book, NULL until ladder capture (B4)

polls(id, started_at, kind{watch|scan|break|verify|focus}, n_quotes, n_opps)

opportunities(id, poll_id, fingerprint, kind, category, event_key,
       ⊕ position_key TEXT, ⊕ venue_pair TEXT,
       edge, ⊕ edge_points TEXT,    -- p̂ − c (or 1 − S); the STORED primary edge (EV#4)
       ⊕ eligible INTEGER, ⊕ elig_reason TEXT, ⊕ lane TEXT,
       ⊕ skew_secs TEXT, ⊕ adj_edge_points TEXT,   -- continuous skew haircut (C1)
       status, legs_json, notes)

flat_bets(  -- append-only; V1 columns kept, additions below
       ... all V1 columns ...,
       ⊕ position_key TEXT, ⊕ venue_pair TEXT,
       ⊕ edge_points TEXT,          -- stored on ARB rows too (R6); ratio is display-only
       ⊕ lane TEXT,                 -- 'clean' | 'review' | 'invalid'  (from eligibility)
       ⊕ elig_reason TEXT,
       ⊕ depth_basis TEXT,
       ⊕ kalshi_leg_result TEXT, ⊕ polymarket_leg_result TEXT, ⊕ sx_leg_result TEXT,  -- A2
       ⊕ reconcile_status TEXT,     -- 'provisional' | 'reconciled' | 'unreconcilable'
       ⊕ se TEXT,                   -- consensus SE carried on EV rows (EV#3)
       ⊕ calibration_status TEXT)   -- per-segment ECE verdict (EV#10)

consensus(event_key, outcome, p_hat, p_hat_calibrated, dispersion, se, n_books,
       devig_method, computed_at)  -- ⊕ p_hat_calibrated, se, n_books, devig_method

annotations(id, flat_bet_id, reason, created_at)   -- immutable corrections (V1)
suspect_matches(...)                               -- V1; rejected joins, never silently dropped
```

**No `UPDATE`/`DELETE` on `flat_bets`.** Reconciliation writes new rows / annotation rows;
`reconcile_status` transitions are new rows keyed by `position_key`, not in-place edits.

---

## 6. Config schema — new/changed keys (deltas from V1 `config.yaml`)

Absent blocks must reproduce V1 behavior (behavior-preserving defaults). New keys:

```yaml
# --- F4 de-vig / calibration ---
devig_method: power              # EMPIRICAL WINNER on V1 MLB settled (Brier 0.2231 / logloss
                                 # 0.6323 vs proportional 0.2240/0.6355, shin 0.2234/0.6335).
                                 # Margin is within noise at n=191 -> re-run bake-off on V2
                                 # sample as a Phase-2 acceptance check; switching method is a
                                 # VERSIONED change (re-freezes cost vectors), never silent.
calibration:
  method: isotonic               # Platt if a segment has < calibration_min_samples
  fit_on: raw_settled            # settled RAW quotes, not flagged-only (EV#5)
  min_samples: 200
  apply: false                   # applied as p_hat_calibrated once sample clears the gate
# --- F4 p̂-as-distribution (EV#3) ---
ev:
  edge_se_min: 1.0               # require edge_points >= this * SE to stay clean
  se_book_correlation: 0.0       # n_eff = n/(1+(n-1)*rho); widens SE (B6)
  edge_decay_half_life_hours: 6  # EXPONENTIAL staleness decay (EV#7): edge*2^(-age/hl). 0=off
  large_edge_gate: 0.05          # R5: edge_points above this is DOWN-weighted/gated, never
  large_edge_action: downweight  #     ranked toward. 'downweight' | 'drop'
  segments: [baseball_mlb]       # EV scoped to calibrated segments only (R5/EV#10)
# --- F5 friction ---
friction:
  base_buffer: 0.005             # legacy flat taker buffer (cost-per-$1 units)
  slippage_impact_coeff: 0.0     # slip += coeff*(stake/depth)^exp ; 0 = legacy flat (EV#6)
  slippage_exponent: 1.0
  slippage_vol_coeff: 0.0        # + vol_coeff*sigma (volatility term)
  fillable_fraction: 1.0         # per-level takeable haircut for SIZING only (B3/B4)
sx:
  win_fee_rate: 0.05             # R4/D3: charged as win_fee_rate*(1-cost) on every SX leg.
                                 # Keep >0; 0 overstates every polymarket+sx arb.
negrisk:
  fee_model: linear              # C0: linear fee BOTH sides (never Kalshi quadratic on Poly)
# --- F6 capacity honesty ---
capacity:
  arb_size_by_ladder: false      # weighted-ARB sizing gated OFF until real ladders exist
  min_ladder_fraction: 0.5       # capacity=UNKNOWN until this fraction of arbs carry a ladder
  arb_size_safety_frac: 0.5
# --- C1 simultaneity (continuous haircut) ---
detection:
  max_actionable_skew_secs: 10
  min_adj_edge_points: 0.002
  line_velocity_points_per_sec: { default: 0.001, baseball_mlb: 0.0008, basketball_nba: 0.0015 }
  doubleheader_cluster_tol_secs: 7200   # D2
# --- §7 feeds ---
feeds:
  polymarket_ws: true            # keyless market channel (default ON)
  kalshi_ws: false               # signed read-only handshake; opt-in (credential decision)
  sx_realtime: false             # realtime-token (API key) since Ably deprecated 2026-07-01
```

`trusted_pairs` becomes a per-market-type map, e.g.
`{ "kalshi+polymarket:WINNER": [baseball_mlb] }`, empty by default (nothing auto-filled).

---

## 7. Venue feeds — the credential decision (frozen)

Public reads only; no order endpoint on any venue. The fastest feeds now involve read-only
credentials for two venues; the frozen decision per §0 of the role doc and data-review §7:

| Venue | Default in V2 | Rationale |
|---|---|---|
| **Polymarket** | **WebSocket** `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribe by `token_id`; **keyless**; maintain a live local book; auto-reconnect; REST `/book` fallback | Keyless, cleanest, and gives the per-token ladder weighted-ARB needs (B4/R8). Build first. |
| **Kalshi** | **REST** `/markets`, `/markets/{t}/orderbook` (poll) by default; WS (`orderbook_delta`, signed `KALSHI-ACCESS-*` handshake) is **opt-in** behind `feeds.kalshi_ws` | WS is read-only but needs credentials — relaxes "unauthenticated GETs only." Off until the user approves a read-only key. |
| **SX Bet** | **REST** `/markets/active`, `/orders` (poll) by default; realtime (Centrifugo, `/user/realtime-token`, API key) opt-in behind `feeds.sx_realtime` | Ably deprecated 2026-07-01; realtime is no longer keyless. REST is the keyless fallback. |
| The Odds API | REST, keyed, credit-limited (§2 rule 3) | Consensus for EV; not latency-critical. |
| MLB Stats API | `statsapi.mlb.com/api/v1/schedule`, keyless | Settlement source; unlimited backfill. |

**Absolute rule preserved:** every credential above is read-only market data; no order/account
call exists in the codebase. Default posture (Polymarket WS + Kalshi/SX REST) needs **zero
credentials** beyond the Odds API key — the WS/realtime keys are the user's explicit opt-in.

---

## 8. Math (frozen formulas + methodology)

All vectors are in the appendix (§14), computed by `tests/vectors_v2/spec_v2_vectors.py`.

### 8.1 De-vig (per book)
- **proportional:** `p̂ᵢ = (1/oᵢ)/Σ(1/oⱼ)`.
- **power (default):** solve `k` so `Σ(1/oᵢ)^k = 1`; `p̂ᵢ = (1/oᵢ)^k`. Bisection on `k∈[0.2,5]`.
- **shin:** `p̂ᵢ = [√(z² + 4(1−z)qᵢ²/B) − z] / (2(1−z))`, `qᵢ=1/oᵢ`, `B=Σqᵢ`; solve `z` so `Σp̂ᵢ=1`.

Method chosen by minimizing Brier/log-loss on settled outcomes (done; power won — §14).
Switching `devig_method` re-freezes cost vectors → versioned change.

### 8.2 Consensus as a distribution (EV#3)
- `p̂ = median` of per-book de-vigged probs across `≥ min_books_for_consensus` books, then
  renormalized so the outcome set sums to 1.
- `dispersion = pstdev` of per-book probs; `SE = dispersion / √n_eff`,
  `n_eff = n / (1 + (n−1)·ρ)` (ρ = `se_book_correlation`, default 0 → `SE = pstdev/√n`).
- **The EV gate is `edge_points / SE ≥ edge_se_min`**, not raw edge (EV#3).

### 8.3 Edge (EV#4)
- **`edge_points = p̂ − c`** is the **stored primary edge** everywhere (ARB rows too, R6),
  where `c = cost + fee_cost + slippage`. For ARB, `edge_points = 1 − S`.
- The ratio `(p̂ − c)/c` is **display-only** — it explodes on cheap contracts; never consumed
  by sizing or ranking.
- **Large-edge handling is INVERTED (R5):** win rate falls as apparent edge rises. Edge above
  `large_edge_gate` is down-weighted or dropped (`large_edge_action`); sort-by-edge ranking is
  **forbidden**.

### 8.4 Calibration (EV#5)
- Fit isotonic (PAV) on settled **raw** quotes (not flagged-only); Platt if
  `n < calibration.min_samples`. Apply as `p̂_calibrated` once `calibration.apply` and the
  per-segment ECE gate pass. Per-segment ECE/Brier-skill gate blocks a segment even if ROI
  passes (EV#10).

### 8.5 Staleness decay (EV#7)
- **Exponential:** `edge_decayed = edge_points · 2^(−age_hours / edge_decay_half_life_hours)`.
  Continuous, not a binary cliff. `half_life = 0` disables.

### 8.6 Fees
- **Kalshi (quadratic):** cost-unit `fee = rate·P·(1−P)`; money `= ceil_to_cent(rate·contracts·P·(1−P))`.
- **Polymarket (linear):** `fee_cost = rate·cost` (sports rate 0.03, crypto 0.07, else 0.05).
- **SX (maker-side + winning fee):** `taker_cost = 1 − percentageOdds/1e20`; **plus**
  `win_fee = win_fee_rate·(1 − taker_cost)` on every SX leg (R4/D3). Effective SX cost =
  `taker_cost + win_fee`.
- **Neg-risk (C0):** **linear** fee on **both** sides — never Kalshi quadratic on Polymarket.

### 8.7 Friction / slippage (EV#6, F5)
- `effective_cost = cost + fee_cost + slippage`.
- `slippage = base_buffer + slippage_impact_coeff·(stake/depth)^slippage_exponent + slippage_vol_coeff·σ`.
- Defaults reproduce V1's flat buffer (`impact_coeff = 0`). `depth_usd` is normalized per venue
  to **taker \$-notional executable at this level**, with a `fillable_fraction` haircut for
  sizing; **single-outcome depth never falls back to event-level liquidity** (B3/D4 → `depth_basis`).

### 8.8 Continuous skew haircut (C1)
- `adj_edge_points = edge_points − skew_secs · line_velocity_points_per_sec[sport]`.
- REVIEW if `skew_secs > max_actionable_skew_secs` **or** `adj_edge_points ≤ min_adj_edge_points`.
  Not a binary "60 s = simultaneous" tag.

### 8.9 True arb
- Best effective cost per outcome `cᵢ`; `S = Σcᵢ`; arb iff `S < 1`; `ROI = 1/S − 1`;
  `stakeᵢ = B·cᵢ/S`. ROI > ~4% ⇒ treat as stale/settlement trap, not celebration (§12 V1).

### 8.10 Divergence
- `spread = mid_A − mid_B` per outcome; DIVERGENCE if `|spread| ≥ divergence_threshold` and no
  arb survives asks+fees. Persist all spreads, `stake = 0`.

---

## 9. Detection rules per class

Every class calls `eligibility(...)` **at detection** and tags `lane`. Every quote is persisted
even when no opportunity is found (rule 7). Every reject writes a diagnostic/suspect row (E2).

- **ARB.** `S = Σ best effective cost`; arb if `S < 1`. Apply skew haircut (§8.8), started/final
  guard (C5), doubleheader clustering (D2, §4.1). Cross-venue → `review` unless trusted pair.
  Store `edge_points`, `venue_pair`, `position_key` on the row (R6/R3/F3).
- **NEGRISK.** Intra-Polymarket exhaustive-exclusive basket; **linear fee both sides** (C0);
  `edge = 1 − Σ eff_cost`; single-venue/single-settlement → `trusted=1`.
- **DIVERGENCE.** §8.10; `stake = 0`; recorded with full quotes.
- **EV.** §8.2–8.5; gate on `edge_points/SE`; large-edge inversion (R5); scoped to
  `ev.segments`; **betting disabled** — every EV flag is recorded, none is auto-bet until the
  pre-registration gate clears.

**Flash verifier (R1).** After a fresh ARB flag, re-quote the exact legs ~10 s later
(`verify.poll_secs`). Write the re-quote's price into **`confirmed_cost`** — this *is* the
confirm source for arbs (fixes the empty confirm column, 5→most). SX legs that can't be
fast-checked are marked `unverifiable`.

---

## 10. Ledger & dual-fill semantics

Append-only `flat_bets`; one row per class per `position_key` first sighting.
- **ARB:** \$1 total split by §8.9 stakes; `edge_points` stored; `locked_pnl` at entry.
- **EV:** \$1 on the single leg; settles at event outcome; `se` and `calibration_status` stored.
- **DIVERGENCE:** `stake = 0`.
- **Dual fill:** `pnl_optimistic` = first-sighting price; `pnl_confirmed` = the **flash-verifier
  re-quote price** (R1) for arbs / second-consecutive sighting for EV; NULL if never confirmed.
- **R2 (survivorship):** the **headline is full-sample OPTIMISTIC**. Confirmed is shown **only as
  a paired delta with a survivorship label** — never as a standalone total (the +\$39 confirmed
  vs −\$20 optimistic gap is selection, not fill quality; drift is flat).
- **Lifetime/dedup:** active while observed on consecutive polls; absent ≥1 full poll → closed;
  reappearance → new row (`reappearance_of`) but same `position_key` for aggregates (§4.2).

---

## 11. Settlement & reconciliation (Phase 6 — where V1 died)

- **Shared settle-eligibility (A1):** settle imports `eligibility` (§3.2); only `lane=="clean"`
  bets settle into headline P&L; REVIEW settles into the review lane only; INVALID never settles.
- **Timing guard at settle too (A3):** re-check `placed_at < start_utc` at settle, not only
  detection.
- **Venue-composed reconcile (A2):** `kalshi_leg_result`, `polymarket_leg_result`,
  `sx_leg_result` resolvers; a bet reconciles iff **every** leg resolves. No leg-type may skip
  reconcile (V1's 348 stuck OPEN arbs). P&L labeled **`provisional`** until reconciled, then
  `reconciled`; unresolvable legs → `unreconcilable` (counted as neither win nor loss).
- **Attribution:** P&L to the EST day the bet was placed; restated days marked `*`. Capital held
  until the **later venue's settlement** time (C4), not event start.
- **Frozen tests:** T-ELIG-1/2 (§3.3); a Polymarket-leg bet AND an SX-leg bet each actually
  reconcile (not skipped); `report --daily` recomputes idempotently from raw rows.

---

## 12. Reporting & the honest-variant matrix

Every P&L number is selectable on three axes so the UI can never quietly show the flattering one:
**optimistic vs confirmed × raw vs clean × flat-\$1 vs quarter-Kelly.** Rules:

- **Headline = full-sample optimistic / clean / flat-\$1**, labeled as such (R2). Confirmed only
  as a paired delta with the survivorship warning.
- **Every arb table/card/aggregate is split by `venue_pair`** (R3) — kalshi+poly (~88% vanish)
  and poly+sx (~5% vanish, unmodeled frictions) never share a win rate.
- **Kelly+confirmed is never a standalone total** (R7) — badge it; it's the single most
  misleading cell (survivorship × weighting).
- **Empty A/B panels hidden**, not shown half-empty. One live surface (`:8787`); retire the
  stale static export (R7).
- Every aggregate **dedups by `position_key`** and filters to `lane=="clean"` for the clean
  column first (§4).
- A version row is either *distinct bets* or a *re-grade under a different rule* (e.g.
  sharp-weighted vs equal-weight consensus) — a re-grade is never presented as independent
  evidence.

---

## 13. Capacity honesty rule (F6)

- Capacity returns **`UNKNOWN` (`INSUFFICIENT_DEPTH_DATA`)** while `n_with_ladder = 0` or while
  `< min_ladder_fraction` of feasible arbs carry a real multi-level ladder. **Never a headline
  number before that** (kills the "\$72,209" fantasy, B1/B2).
- Weighted-ARB / Kelly-ARB sizing (`ladder_scaled_stake`) stays gated behind
  `capacity.arb_size_by_ladder: false` until (1) Polymarket per-token `/book` ladders are
  captured and normalized (B4/R8), and (2) the capacity report shows genuine *edge*-limited
  arbs. On a single flat rung, profit is linear and the lock never crosses — the model literally
  cannot see the losing region, so the weighted column would compare against an empty one.
- **T-CAP-1:** with no ladders, capacity report prints `UNKNOWN`, never a dollar figure.

---

## 14. Test-vector appendix (frozen; reproduced by `tests/vectors_v2/spec_v2_vectors.py`)

All values to 6 dp unless money (2 dp). Money in `Decimal`.

**De-vig — proportional**
- `(1.91, 1.91)` → `(0.500000, 0.500000)`; implied-sum `1.047120`.
- `(1.50, 2.86, 6.00)` → `(0.563547, 0.295567, 0.140887)`; k(=Σ1/o) `1.182984`.

**De-vig — power** (default): `(1.50, 2.86, 6.00)` → k `1.219495`, p `(0.609899, 0.277628, 0.112473)`.
**De-vig — shin:** `(1.50, 2.86, 6.00)` → z `0.095430`, p `(0.593870, 0.289348, 0.116782)`.

**Consensus + SE** (3 books, decimal odds home/away: A(1.80,2.10) B(1.91,1.91) C(2.00,1.83),
proportional de-vig): per-book home p `(0.538462, 0.500000, 0.477807)`; median home `0.500000`,
away `0.500000`; renorm `(0.500000, 0.500000)`; dispersion(pstdev) `0.025057`; SE `0.014467`.

**Edge / EV** (p̂=0.55, Kalshi ask 0.50, fee 0.07·0.5·0.5=`0.017500`, c=`0.517500`):
`edge_points = 0.032500`; EV_ratio (display) = `0.062802`.

**Kalshi fee:** unit @P=0.50 `0.017500`; money 10 contracts @P=0.40 raw `0.168000` → ceil `0.17`.
**Polymarket linear fee:** rate 0.03 · cost 0.50 = `0.015000`.
**SX:** percentageOdds 5.5e19 → taker cost `0.450000`; win_fee 0.05·(1−cost) = `0.027500`;
effective SX cost `0.477500`.

**True arb** (odds 2.10 & 2.05, B=\$100): cost_a `0.476190`, cost_b `0.487805`, S `0.963995`,
ROI `0.037349`; stake_a `49.397590` (\$49.40), stake_b `50.602410` (\$50.60); both payouts
`103.734940` (\$103.73). **No-arb** 1.95/1.95: S `1.025641`.

**Divergence:** mid_A 0.58 − mid_B 0.53 → spread `0.050000`.

**Depth-aware slippage** (base 0.005, impact 0.02, stake 50, depth 500, exp 1): slip `0.007000`;
legacy (impact 0) `0.005000`.

**Staleness decay** (edge 0.03, half-life 6h, age 3h): decayed edge `0.021213`.

**Edge/SE gate:** edge 0.02 / SE 0.015 = `1.333333` → PASS (≥1.0); / SE 0.025 = `0.800000` → FAIL.

**Isotonic (PAV)** raw p̂ `[0.2,0.3,0.4,0.5,0.6,0.8]`, y `[0,0,1,0,1,1]` → fitted
`[0.0, 0.0, 0.5, 0.5, 1.0, 1.0]`.

**Neg-risk basket** (asks 0.30/0.34/0.33, linear +3% both sides): eff
`(0.309000, 0.350200, 0.339900)`, S `0.999100`, edge `0.000900`.

---

## 15. Phase-by-phase acceptance commands

Order strict; a phase starts only after the prior one's acceptance passed and VERIFY signed off.
PLAN writes spec+tests → CODE makes `pytest` green (no vector edits) → VERIFY reviews diff.

| Phase | Build | Acceptance |
|---|---|---|
| 1 Skeleton & config | package, config loader (§6 schema), `models.py` (position_key, venue_pair, eligibility fields), store (WAL, §5 schema), empty `report` | `arb report` prints empty frame; DB created; `pytest` green; **no REAL money column** |
| 2 Math core | `pricing.py` — all §8 formulas | all §14 vectors green; `edge_points` (not ratio) stored; `Decimal` throughout; **de-vig bake-off re-run** on V2 sample confirms/updates `devig_method` |
| 3 Venue adapters | Kalshi/Poly/SX fixtures-first; normalized `depth_usd`+`depth_basis`; Poly per-token ladder; Kalshi start-unknown; SX inversion+win-fee; Polymarket WS | fixtures parse; SX inversion test predates SX pipeline; `watch --once --dry-run` shows quote counts; credits logged |
| 4 Matching | aliases, 6h bucket, exact line, doubleheader clustering (D2), sanity gates | same MLB game 3 venues → 1 event_key; doubleheader → 2; suspect rows written not dropped |
| 5 Detection + display | `detect_true_arbs` calling `eligibility` at detection; skew haircut; neg-risk linear; flash verifier → `confirmed_cost`; persist every quote | `watch --once` end-to-end; quotes persisted; REVIEW never in clean lane |
| 6 Ledger/settle/reconcile ⚠️ | tracker (dual-fill, position_key), shared settle-eligibility (A1), timing guard (A3), venue-composed reconcile (A2), daily CSV/MD | T-ELIG-1/2/3/4/5 pass; tracker vector to the cent; 3-poll fixture → correct CSV; `report --daily` idempotent |
| 7 EV (measure only) | calibrated p̂, edge/SE gate, exp decay, large-edge inversion, CLV vs no-vig mid, per-segment calibration | EV flags carry `edge_points`, `se`, `calibration_status`; nothing auto-bet |
| 8 Capacity/exposure/reporting | capacity UNKNOWN rule (F6), exposure in scan (C3), C4 horizon, honest-variant matrix, venue-pair split | capacity honest-UNKNOWN or real edge-limited; aggregates dedup by position_key + filter to clean |

---

## 16. Traceability — R1–R8 and weakness → section

| Fix | Where |
|---|---|
| R1 flash-verify price = `confirmed_cost` | §9, §10 |
| R2 optimistic headline, confirmed as delta | §10, §12 |
| R3 venue_pair split everywhere | §4.1, §5, §9, §12 |
| R4/D3 SX honest (win fee + real depth) | §8.6, §6 |
| R5 EV edge inversion; MLB-scoped; Poly-first | §8.3, §9 |
| R6 leg resolvers; `edge_points` on ARB; dedup | §5, §9, §11 |
| R7 UI guardrails (Kelly+confirmed, panels, one surface) | §12 |
| R8 WebSocket book reads (Poly first) | §7 |
| A1 shared settle-eligibility | §3, §11 |
| A2 multi-venue reconcile | §11 |
| A3/C5 timing guard | §3, §11 |
| B1/B2 capacity honesty | §13 |
| B3/B4/D4 depth_basis, ladder, no event fallback | §5, §8.7 |
| C0 linear neg-risk fee | §8.6, §9 |
| C1 continuous skew haircut | §8.8 |
| C2/F3 position_key | §4 |
| C4 capital to settlement | §11 |
| D1 start-unknown | §3, §6 |
| D2 doubleheader clustering | §4.1, §6 |
| EV#1 de-vig choice | §8.1, §14 |
| EV#3 p̂ distribution / edge-SE gate | §8.2, §8.3 |
| EV#4 edge_points primary | §8.3, §5 |
| EV#5 calibration | §8.4 |
| EV#6 depth-aware slippage | §8.7 |
| EV#7 exponential decay | §8.5 |
| EV#9 CLV vs no-vig mid | `PREREGISTRATION_V2.md` §3 |
| EV#10 per-segment calibration gate | §8.4, §12 |

---

## 17. Definition of done (the whole project's single gate)

V2 is "better than V1" only when a VERIFY pass confirms: **the same eligibility predicate object
gates detection, fill, settle, reconcile, capacity, and exposure, with a passing test (T-ELIG-1)
that a REVIEW arb never reaches clean/headline P&L** — plus R1 (arbs have a real `confirmed_cost`)
and R2 (no survivorship headline) hold in the shipped reports.
