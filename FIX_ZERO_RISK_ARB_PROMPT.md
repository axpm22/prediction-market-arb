# Fix prompt for Claude Code — make the zero-risk arb book *real*, then size it

**Read `CLAUDE.md` and `SPEC.md` first. They override anything here.** This prompt is the work
order; it does not relax any hard rule.

## What "maximize profit at zero risk" actually means in this repo

Only **TRUE ARB** locks profit at entry. EV and DIVERGENCE carry outcome risk → out of scope.
So the entire job is: *make the true-arb book genuinely zero-risk, then size into it.* The
live ledger (`data/arb.sqlite`) says it is **not** zero-risk today:

- 314 settled arbs, **all `trusted=0`** (cross-venue REVIEW) — exactly the population hard
  rule #6 forbids counting. `trusted_pairs: []` in `config.yaml`, so by construction nothing is
  actionable.
- `pnl_confirmed ≈ $0.00` across the whole book. The ~$5 of "profit" is all `pnl_optimistic`
  (first-sighting price). Per SPEC §9 the optimistic−confirmed gap **is** the slippage, and it is
  currently eating 100% of the edge.
- 148 distinct fingerprints across 1,276 ARB rows (~5× re-counting).

**Therefore the lever is not bigger stakes. It is raising the share of the book that is
genuinely `trusted=1` and confirmed-fillable — today ~0%.** Phases 1–2 do that; Phase 4 sizes
only what survives. Do not skip ahead to sizing.

---

## Phase 0 — VERIFY current state before writing anything (do not redo done work)

The audit docs (`ARB_WEAKNESSES.md`, `ARB_LADDER_AND_FRICTIONS_SPEC.md`) are partly stale — much
is already implemented. Confirm each item's real state and write a one-line PASS/OPEN verdict per
item to `logs/fix_audit.md` before touching code. Known-as-of-this-prompt:

- **A1 (REVIEW lane at settle)** — appears DONE (`settlement.py` writes `quality_flag='REVIEW'`
  for `trusted=0`). Verify clean aggregations exclude it.
- **A2 (per-venue resolvers + reconcile dispatch)** — code exists: `kalshi_leg_result`,
  `polymarket_leg_result`, `sx_leg_result`, `reconcile.default_resolvers()`. Verify (a) reconcile
  runs automatically after `settle` in `cli.py` **and** the daemon, and (b) each resolver actually
  returns WON/LOST/VOID on a recorded settled fixture (not always None).
- **C0 (neg-risk linear fee)** — DONE (`negrisk.py` uses `all_in_fee_cost(..., basis="linear")`).
  Verify the break-even unit vector exists.
- **B4 (Polymarket multi-level ladder)** — DONE (`polymarket.py:polymarket_ladder`,
  `_attach_clob_books`). Verify the capacity report's captured-ladder fraction is now > 0 on a
  live scan.
- **D3 (SX winning-bet fee)** — likely OPEN (`config sx.fee_rate: 0.0`, no `win_fee` term in
  `sx.py`). Confirm.
- **C1 / C3 / C4 / D1 / D2** — likely OPEN. Confirm against the code.

Only implement items your Phase 0 verdict marks OPEN. For items marked PASS, add the missing
**test** if one is absent, and move on.

Per CLAUDE.md workflow: after every phase run `pytest`, then the phase's acceptance command, and
paste the output.

---

## Phase 1 — THE GATE: whitelist exactly one trusted pair (the only source of real zero-risk P&L)

Target pair: **`kalshi+polymarket:baseball_mlb`** (single-oracle-free, simplest void rules, both
venues verifiable). Nothing here is "just code" — a cross-venue lock is zero-risk **only** if both
venues resolve the identical event identically. This phase produces the evidence to justify the
one-line config change, and the regression test that protects it.

### 1a. Settlement-rule comparison (research deliverable)
Write `docs/settlement_parity_kalshi_polymarket_mlb.md` comparing, with a cited source per row
(public docs only — Kalshi rules pages, Polymarket market resolution / UMA docs):

- Regulation vs extra innings: does each settle on final score incl. extra innings?
- Postponement / suspension: void-and-refund vs carry-over vs settle-as-of. **If the two venues
  differ here, the pair is NOT parity-safe — stop and report.**
- Rain-shortened / called games: official-game threshold each venue uses.
- Push/tie handling (moneyline can't tie in MLB, but confirm).
- Resolution source/oracle and dispute window per venue (Polymarket UMA dispute lag matters for
  C4 holding period).

Output a verdict line: `PARITY: SAFE` or `PARITY: UNSAFE (reason)`. Do **not** proceed to 1c
unless SAFE.

### 1b. Prove the resolver loop closes on real settled games
Pick 3–5 already-settled MLB events that appear in `data/arb.sqlite` with both a Kalshi and a
Polymarket leg. Run `reconcile_settlements(store, default_resolvers())` against them (public GETs
only — no credits). Assert every leg resolves to WON/LOST/VOID (none None) and that
`reconcile_arb` returns `agrees=True` (`wins==1, voids==0`) for clean locks. If any leg returns
None, fix that venue's resolver before continuing — an unverifiable leg can never be zero-risk.

### 1c. Whitelist + protect
Only after 1a `PARITY: SAFE` **and** 1b reconciles clean:

- Set `config.yaml` `trusted_pairs: ["kalshi+polymarket:baseball_mlb"]`.
- Confirm the chain: `status_for_legs` → `OK` → tracker writes `trusted=1` →
  `_open_arb_bets`/settle path books it into the **clean** (non-REVIEW) lane.
- **Regression test** (`tests/test_trusted_gate.py`): a fixture with one
  `kalshi+polymarket:baseball_mlb` arb (whitelisted → `trusted=1`, clean settle row) and one
  `polymarket+sx` arb (not whitelisted → `trusted=0`, `quality_flag='REVIEW'`, excluded from clean
  aggregations). Assert headline P&L includes only the first.

**Acceptance:** `arb settle` then `arb report --daily` shows a non-zero **trusted=1** settled arb
line, and `reconcile` produces a `RECONCILED / AGREE` child row for it. Headline still excludes all
`trusted=0` rows.

---

## Phase 2 — Make `pnl_confirmed` real (kill the mirage)

`pnl_confirmed` being ~$0 everywhere means the confirm path never books the second-sighting fill,
so we cannot tell edge from mirage. Per SPEC §9 the confirmed column = fill at the **second
consecutive** observation; NULL if never seen twice.

1. **Audit the promote/confirm path** (`lifecycle.py` / `tracker.py`): on a fingerprint's second
   consecutive sighting, it must re-quote both legs and write `confirmed_cost` / `pnl_confirmed`
   from those fresh prices; if `Σ effective_cost_fresh ≥ 1` the lock didn't hold → do **not**
   promote (leave confirmed NULL = "missed"). Fix whatever breaks this for the trusted pair.
2. **C1 simultaneity haircut** (continuous, per `ARB_LADDER_AND_FRICTIONS_SPEC.md §1`): replace the
   binary `time-skew` tag with `adj_edge = edge_points − skew_secs · v_sport`; `REVIEW` if
   `skew_secs > max_actionable_skew_secs` (10s) or `adj_edge ≤ min_adj_edge_points`. Carry
   `adj_edge` (not raw edge) into ranking and sizing. Add the config block and the four test
   vectors from that spec.

**Acceptance:** after a live `watch` window over MLB, trusted-pair arbs show **non-null
`pnl_confirmed`**, and `arb report` prints the optimistic-vs-confirmed (mirage) gap. The only
profit you may treat as real from here on is the **confirmed, trusted=1** column.

---

## Phase 3 — Friction correctness that changes which arbs are even real

Implement only those Phase 0 marked OPEN. Each needs the unit vector before the adapter uses it
(CLAUDE.md #8/#10).

- **D3 — SX winning-bet fee** (`ARB_LADDER_AND_FRICTIONS_SPEC.md §6`): an arb always has exactly
  one winning leg; SX charges on winnings, so the SX leg's fee is currently understated as 0. Add
  `sx.win_fee_rate` to config; charge the conservative `fee_cost_sx = q6(win_fee_rate · (1 − c))`
  on SX legs; recompute `total_cost`/ROI. Vector: an arb that is +EV at zero SX fee but break-even
  at `win_fee_rate=0.05` must NOT flag. (Keep the SX adapter's existing refuse-while-null guard.)
- **D1 — Kalshi start fallback** (`§4`): never fall back to `close_time` for `start_utc`; a ticker
  whose time can't be parsed gets `start-unknown`, `start_utc=None`, and is non-actionable.
- **D2 — doubleheader clustering** (`§5`): inside `detect_true_arbs`, cluster a market group's legs
  by `start_utc` (≤2h) before pairing outcomes so game-1 and game-2 legs never combine.

**Acceptance:** `pytest` green incl. the new vectors; re-run detection and confirm the count of
flagged trusted-pair arbs changes in the expected direction (some SX/poly arbs correctly drop out).

---

## Phase 4 — ONLY NOW size for maximum profit (still zero-risk)

Sizing is meaningless until Phases 1–3 give a trusted, confirmed, slippage-aware book. Then:

1. **Capacity must read real ladders** (`B1/B3` + `arb_capacity` UNKNOWN gate): the report prints
   `INSUFFICIENT_DEPTH_DATA` while captured-ladder fraction `< min_ladder_fraction`. Run live MLB
   scans until trusted-pair arbs carry real Polymarket+Kalshi ladders, then confirm the report
   shows genuine **edge-limited** break-evens (not always depth-limited).
2. **Turn on sizing**: set `arb_size_by_ladder: true` with a conservative `safety_frac`. Stake off
   `profitmax_stake_usd × safety_frac` (already in `ladder_scaled_stake`) using the
   **simultaneity-adjusted** edge from Phase 2.
3. **C3 — per-venue capital cap binds detection** (`§2`): capital is siloed per venue (you fund
   both legs and can't move USDC instantly), so allocate greedily by `adj_edge` subject to
   `bankroll` and each `per_venue_caps[venue]`; a cross-venue arb decrements **every** venue it
   touches; below `min_executable_usd` → `REVIEW capital-capped`.
4. **C4 — charge capital to settlement, not start** (`§3`): `hold_days = max across legs of
   (start + game_duration + venue_settlement_lag)`; feed into `is_economic` so tiny far-dated locks
   tag `uneconomic`.

**Acceptance:** for the trusted pair, capacity stops printing `INSUFFICIENT_DEPTH_DATA`; sizing
scales up with edge×depth, never exceeds any `per_venue_cap` or `bankroll × max_stake_pct`, and a
0.2% lock 14 days out is tagged `uneconomic`. Headline daily P&L = trusted=1, confirmed, economic.

---

## Hard-rule guardrails (restate, do not violate)

- Paper only; public **unauthenticated GETs** only (reconcile/resolvers included). Never spend Odds
  API credits in loops/tests — fixtures only; abort scan if `x-requests-remaining < 50`.
- All money/price math in `Decimal` (`pricing.money`, `q6`); float for display only.
- `flat_bets` is **append-only** — reconciliation, REVIEW lanes, and corrections are new rows,
  never mutations.
- Cross-venue stays `REVIEW` unless its pair is in `trusted_pairs`. Phase 1 whitelists exactly one
  pair, only after parity is proven. Do not whitelist a second pair without its own §1a/1b.
- No invented endpoints/fields (SPEC §3). If a venue read 4xx's, stop and report.
- Every threshold/rate/cap lives in `config.yaml` — no magic numbers.

## Definition of done

`arb report --daily` shows a headline number that is **trusted=1, confirmed-fill, economic,
reconciled-AGREE** and nothing else; every `trusted=0` row is visibly quarantined as research; and
`arb arb-capacity` reports real edge-vs-depth limits for the whitelisted pair. That headline number
— and only that — is the profit you can safely size into.
