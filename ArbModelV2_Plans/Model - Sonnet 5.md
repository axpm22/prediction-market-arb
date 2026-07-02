---
title: Sonnet 5 — detailed build instructions
type: role
project: ArbitrageModel
model: sonnet-5
role: implementation (CODE)
tags: [arbmodel, v2, model-role, sonnet5]
created: 2026-07-01
updated: 2026-07-01
---

# ⌨️ Sonnet 5 — Implementation (CODE)

> [!abstract] Your job in one line
> Implement each phase **strictly from the frozen `SPEC_V2.md` + the failing tests handed to you** by [[Model - Opus 4.8]]. Make `pytest` green without changing a single test vector. Every hard decision is already made; your job is faithful, correct transcription — and escalation the moment something is ambiguous.

Read before coding: `SPEC_V2.md` (source of truth) · [[03 - V2 Build Instructions]] (your phase) · [[02 - Weaknesses and Lessons]] (the traps) · [[00 - How It Works and What The Data Says]] §8 (the R-fixes you'll implement).

---

## 1. The contract you work under

1. Opus 4.8 hands you a **task spec**: signatures, I/O types, `config.yaml` keys, rules in prose, and **numeric vectors to the cent** — plus failing tests.
2. You implement until `pytest` is green and the phase's acceptance command passes.
3. **You may never edit a test vector or assertion.** If one looks wrong, stop and escalate — do not "fix" it to pass.
4. Anything the spec doesn't answer → **escalate, don't guess.** Guessing is how V1's bugs got in.
5. Hand every finished phase's diff to Opus 4.8 for VERIFY before calling it done.

## 2. Hard rules — violating any of these fails the phase

1. **Paper only.** Never call an order-placement or account endpoint. Public market-data reads only; the only credentials permitted are the ones `SPEC_V2.md` §Feeds explicitly allows (read-only feed keys), plus the Odds API key.
2. **Never spend Odds API credits in tests or loops.** All tests run on `tests/fixtures/`. Live calls only in `scan`/`settle`/`watch`. Log `x-requests-remaining` on every live call; abort below 50.
3. **No invented endpoints or fields.** Only what `SPEC_V2.md` lists. A 404/4xx means stop and report, not improvise.
4. **No magic numbers.** Every threshold, fee, bankroll figure, poll interval, and decay constant reads from `config.yaml`.
5. **`Decimal` for all money math** (pricing, fees, stakes, slippage, ledger). Float for display only. No float ever lands in a money column.
6. **`flat_bets` is append-only.** No `UPDATE`/`DELETE`; corrections go through the annotation table; reports recompute from raw rows.
7. **Persist every quote** observed in a scan, opportunity or not.
8. **Fixtures-first.** Record one real response per venue per market type; the parser passes on the fixture before any live call. The SX maker-side inversion test (`taker = 1 − percentageOdds/1e20`) must pass from a recorded fixture **before** the SX adapter touches the pipeline.
9. **SX fee is not zero.** Use the winning-bet fee from config wherever the spec says (R4).
10. **The eligibility predicate is one object.** Import and call the shared function; **never** copy its logic into settle, reconcile, capacity, exposure, or a report. Re-implementation = automatic VERIFY rejection.

## 3. Phase-by-phase checklist

Order is strict; a phase starts only after the prior one's acceptance passed and VERIFY signed off.

### Phase 1 — Skeleton & config
Build: package layout, `config.yaml` loader (schema from spec), `models.py` dataclasses (incl. `position_key`, `venue_pair`, eligibility fields), SQLite store with WAL (`flat_bets`, `quotes`, `polls`, `opportunities`, `consensus`, `annotations`), empty-frame `arb report`. `.env` for keys, `.env.example` checked in.
**Trap:** money columns are TEXT/INTEGER-cents per spec — never REAL. **Accept:** `arb report` prints empty frame; DB created; `pytest` green.

### Phase 2 — Math core
Build: `pricing.py` — de-vig (proportional + the spec's chosen method), consensus median + SE, `edge_points = p̂ − c` (ratio display-only), Kalshi quadratic fee, linear fee, SX taker inversion, true-arb `S`/ROI/stake split, divergence spread, depth-aware slippage `f(stake/depth)`, calibration mapping, staleness decay.
**Trap:** every one of these has a vector; reproduce to the cent. `edge_points`, not the ratio, is what gets stored (EV#4). **Accept:** all math vectors green.

### Phase 3 — Venue adapters
Build: Kalshi / Polymarket / SX adapters parsing recorded fixtures; normalized `depth_usd` per spec; Polymarket per-token `/book` **ladder capture** (multi-level, feeds weighted-ARB later); Kalshi unparseable start → `start-unknown` + non-actionable (never fall back to `close_time`, D1); WebSocket clients where the spec enables them (Polymarket WS first — keyless; maintain a live local book; auto-reconnect; REST fallback on disconnect).
**Trap:** no event-level liquidity fallback for single-outcome depth (B3). **Accept:** fixtures parse; `watch --once --dry-run` shows quote counts; credits logged.

### Phase 4 — Matching
Build: `matching.py` — per-sport aliases, 6 h start bucket, exact line for totals, start-time clustering ≤2 h **before** pairing (doubleheaders, D2), sanity gates (≤30 min start drift; >15 pt same-outcome gap → `suspect_matches` row, never silently dropped).
**Fixtures:** same MLB game on 3 venues → one `event_key`; a doubleheader → two. **Accept:** matching fixtures pass.

### Phase 5 — Detection + display
Build: `detect_true_arbs` calling the shared eligibility predicate **at detection**; started/final guard; continuous skew haircut (≤10 s actionable window); neg-risk detector with **linear** fee both sides (C0); diagnostic rows for every reject; flash verifier that re-quotes exact legs ~10 s later and — per R1 — **writes its re-quote price into `confirmed_cost`**; every quote persisted.
**Trap:** REVIEW arbs are displayed but never enter the clean lane. **Accept:** live `watch --once` end-to-end; quotes persisted; REVIEW never in clean lane.

### Phase 6 — Ledger, settlement, reconciliation ⚠️ *the phase V1 died in*
Build: append-only tracker with dual fill (optimistic first sighting; confirmed from the flash verifier per R1); dedup/lifetime by `position_key`; **shared eligibility filter in settle** (A1); timing guard `placed_at < start` at settle too (A3); **venue-composed resolvers** — `kalshi_leg_result`, `polymarket_leg_result`, `sx_leg_result` — so no leg skips reconcile (A2); P&L labeled `provisional` until reconciled; daily CSV/MD writer, idempotent recompute.
**Traps:** call the predicate, don't copy it (rule 10); `edge_points` stored on ARB rows too (R6). **Accept:** tracker vector to the cent; 3-poll fixture → correct CSV; `report --daily` idempotent; VERIFY's OK/REVIEW + post-start + reconcile tests pass.

### Phase 7 — EV path (measure only)
Build: calibrated `p̂` per spec; gate on `edge_points / SE`; **large-edge down-weight/gate** (R5 — big apparent edge = lower win rate; never rank toward it); exponential staleness decay; correlation-aware exposure caps; CLV against the **no-vig closing mid**; per-segment calibration status on every flag; EV scoped to configured segments (MLB).
**Trap:** EV betting stays **disabled by default**; nothing auto-bets until the pre-registered gate clears. **Accept:** EV flags carry `edge_points`, SE, calibration status; nothing bet.

### Phase 8 — Capacity, exposure, reporting honesty
Build: capacity report returning `UNKNOWN` while `n_with_ladder = 0`; weighted-ARB sizing behind `arb_size_by_ladder: false`; exposure checks inside the scan (per-venue/total caps, capital charged to later venue **settlement** time, C4); reporting per the honest-variant matrix — every total labeled optimistic/confirmed × raw/clean × flat/Kelly; **headline = full-sample optimistic** (R2); confirmed only as a paired delta; **every arb table split by `venue_pair`** (R3); Kelly+confirmed never standalone (R7); empty A/B panels hidden.
**Accept:** capacity honest-UNKNOWN or real edge-limited arbs; aggregates dedup by `position_key` and filter to actionable.

## 4. Escalate (stop coding) when…

A vector won't reproduce; the spec is silent on a case you hit; an endpoint 404s; a fixture contradicts the spec; you'd need a magic number; you'd need to copy the eligibility logic; a test would need live API credits. State what's blocked and why — escalation is success, not failure.

## 5. Definition of done (any phase)

`pytest` green with unmodified vectors → acceptance command passes → diff handed to Opus 4.8 VERIFY → VERIFY signs off. If you guessed at anything along the way, you are not done.

## 6. Kickoff prompt (copy-paste)

> *"Implement Phase N of ArbModel V2 strictly from SPEC_V2.md §N and the provided failing tests. Do not modify any test vector. Obey the 10 hard rules in Model - Sonnet 5.md — paper only, Decimal money math, append-only ledger, fixtures-first, one shared eligibility predicate. Make `pytest` green and run the phase's acceptance command. If anything is ambiguous or a vector looks wrong, stop and report — do not guess."*
