# ARB ladder capture + frictions — implementation spec

Build-ready logic for the items left open in `ARB_WEAKNESSES.md` after the A1/A2/A3 + B2
fixes:

- **B1/B3** — real multi-level ladder capture + depth-unit normalization (the root of "how
  aggressive before I lose").
- **C1** — simultaneity haircut (continuous, not a binary skew tag).
- **C3** — portfolio / per-venue capital cap that actually binds detection.
- **C4** — holding period charged to settlement, not event start.
- **D1** — Kalshi start-time fallback.
- **D2** — doubleheader leg clustering.
- **D3** — SX winning-bet fee.

Hard-rule constraints carried throughout: `Decimal` for all money/price math (display may
float); append-only ledger (no row mutation); public unaudited reads only; cross-venue stays
REVIEW unless whitelisted. All thresholds live in `config.yaml`.

---

## 0. The canonical ladder contract (shared by B1/B3 and capacity)

Every adapter must emit, per quote, a `ladder` of fee-exclusive levels, best price first:

```
Ladder = list[ (cost_per_$1_payout: Decimal, taker_notional_usd: Decimal) ]
# invariant per level:
#   cost ∈ (0, 1)                          raw ask in the $1-payout unit (NO fee here)
#   taker_notional_usd > 0                 the $ you would STAKE to consume this level
#   contracts_at_level = taker_notional_usd / cost
#   levels strictly sorted by ascending cost (best first), no dups
```

Fees are added once, downstream, by `capacity._leg_ladder` (`raw_cost + leg.fee_cost`) — so the
ladder stays the pure book and the fee model stays in one place. This is already the contract
`capacity.py` assumes; the job is to make all three adapters honor it and to define
`taker_notional_usd` identically across venues.

**Normalization invariant (B3):** `taker_notional_usd` is always "dollars staked to take this
level," never maker stake, never contracts, never event-level liquidity. Cross-venue `min()` in
`arb_capacity` is only valid once every venue reports this same quantity.

Per-venue derivation (verify each against a recorded book in a unit test):

- **Kalshi** (`_ladder_at_ask`, already multi-level): taking YES consumes resting NO bids; a NO
  bid at price `p`, size `s` contracts → YES at `cost = 1 − p`, `taker_notional = s · cost`.
  *Open item:* the size column's units are undocumented (`kalshi.py:227`). Add a calibration
  test that buys a known notional on a fixture and asserts contracts = notional/cost; if size is
  not contracts, scale here, not downstream.
- **SX** (`_taker_ladder`, already multi-level): a maker order with remaining stake `M` at taker
  `cost = 1 − percentageOdds/1e20` backs taker notional `T = M · cost/(1 − cost)`
  (`sx.py:190`). Already correct; keep, and add the same calibration test.
- **Polymarket** (`polymarket.py:120`, **the only real gap**): the batch endpoint gives
  top-of-book only, so the adapter builds a 1-level ladder. Every arb in the book has a
  Polymarket leg, so the binding (thinnest) leg is always single-level → slippage is
  unmodelable book-wide. **Fix:** for arb legs, fetch the per-token CLOB `/book` (the adapter
  already calls `/book` in `_attach_clob_books`) and build the full ladder:

```python
def polymarket_ladder(asks: list[dict]) -> Ladder | None:
    levels = []
    for lvl in asks:                      # CLOB asks: {price, size(shares)}
        price = Decimal(str(lvl["price"]))
        size  = Decimal(str(lvl["size"]))
        if not (0 < price < 1) or size <= 0:
            continue
        levels.append((price, q6(price * size)))   # taker_notional = price·shares ($)
    levels.sort(key=lambda x: x[0])        # best (lowest ask) first
    return tuple(levels) or None
```

**Fillable-fraction haircut (B3):** displayed top-of-book is not 100% takeable (queue, latency,
partial fills). Apply a config haircut when building the *effective* ladder used for sizing:

```
fillable_taker_notional = level_taker_notional × config.arb.fillable_fraction   # e.g. 0.5
```

Keep the raw ladder in storage; apply the haircut only in capacity/sizing so the recorded book
stays honest.

### Why this "can't be fixed by logic alone"

0 of 661 stored legs carry a ladder, and the historical books are gone — they cannot be
backfilled. Two consequences:

1. **Going forward:** once the Polymarket multi-level fetch lands and adapters keep populating
   `ladder` (Kalshi/SX already do), the scan path persists it (`store.quote_to_json` already
   serializes `ladder`). New arbs accumulate real ladders automatically.
2. **Repair current opens (optional `arb recapture` command):** for each still-OPEN arb
   fingerprint, re-fetch each leg's live book, rebuild the ladder, and append a *new annotation
   quote row* (append-only) carrying the ladder. Capacity then reads the latest ladder-bearing
   observation. Settled/closed arbs stay un-repairable — accept it.

```
arb recapture:
  for fp in open_arb_fingerprints():
    legs = latest_legs(fp)
    new_legs = []
    for leg in legs:
        book = adapter(leg.venue).fetch_book(leg.book)     # public GET, no credits
        new_legs.append(leg_with_ladder(leg, build_ladder(book)))
    if all(l.ladder for l in new_legs):
        append_quote_observation(fp, new_legs, note="ladder-recapture")
```

### Capacity report behavior until the fraction is non-trivial

`run_arb_capacity` must stop emitting a fantasy headline:

```
captured = n_with_ladder / max(1, n_feasible)
if captured < config.arb.min_ladder_fraction:        # e.g. 0.5
    breakeven = profitmax = summed_profit = UNKNOWN   # print "INSUFFICIENT_DEPTH_DATA"
    show captured fraction + "top-of-book only, no slippage modeled"
else:
    report break-even / profit-max / edge-vs-depth from real ladders
```

With real ladders, the existing `arb_capacity` math becomes meaningful: profit(N) = N −
Σ vwapᵢ(N) is concave-piecewise-linear; its zero-crossing is the **edge-limited** break-even
(the real "how aggressive before I lose"), and `limited_by` finally distinguishes edge from
depth. Size off `profitmax_stake_usd × safety_frac` (already in `ladder_scaled_stake`), now a
real number.

**Tests:** per-venue ladder normalization vs recorded book (3 fixtures); a synthetic 2-leg arb
with known sloped ladders → assert break-even, profit-max, and `limited_by='edge'`; capacity
report returns UNKNOWN while `captured < min_ladder_fraction`.

---

## 1. C1 — Simultaneity haircut (continuous)

**Goal:** replace the binary `time-skew` REVIEW tag with a continuous edge decay, and bar arbs
whose legs were never live together from being actionable.

**Inputs:** `skew_secs = leg_skew_secs(best_legs)` (already computed, `arbitrage.py:33`);
per-sport line velocity `v_sport` in probability-points/second (config, seeded from observed
consensus/price movement — see calibration).

**Logic (at detection, after computing `edge_points = 1 − Σcost`):**

```
v        = config.arb.line_velocity_points_per_sec.get(sport, default)
haircut  = q6(Decimal(skew_secs) * v)              # points the edge may have decayed
adj_edge = q6(edge_points - haircut)               # simultaneity-adjusted margin

notes += f"skew={int(skew_secs)}s,haircut={haircut},adj_edge={adj_edge}"

if skew_secs > config.arb.max_actionable_skew_secs:    # e.g. 10
    status = "REVIEW"; notes += "skew-nonsimultaneous"
elif adj_edge <= config.arb.min_adj_edge_points:       # e.g. 0.002
    status = "REVIEW"; notes += "skew-edge-eroded"
```

Carry `adj_edge` (not raw `edge_points`) into sizing and into the C3 allocation ranking. Store
both so the report can show the raw-vs-adjusted gap (the mirage, mirroring the optimistic-vs-
confirmed columns).

**Confirm-time recheck:** when an arb is promoted (second sighting), re-quote both legs (the
re-quote machinery exists: `requote_sx_leg`, Kalshi/Polymarket book reads) and require the lock
to still hold on fresh, simultaneous quotes: `Σ effective_cost_fresh < 1`. If not, do not
promote.

**Calibration:** estimate `v_sport` from stored quotes — for each event/outcome, regress
|Δ devigged prob| against Δt between consecutive observations; use a robust slope (median of
per-pair |Δp|/Δt). Recompute monthly; store in config. Until calibrated, use a conservative
default (e.g. 0.001 pts/s pregame, higher in-play).

**Config:**
```yaml
arb:
  max_actionable_skew_secs: 10
  min_adj_edge_points: 0.002
  line_velocity_points_per_sec:
    default: 0.001
    baseball_mlb: 0.0008
    basketball_nba: 0.0015
```

**Tests:** two legs 0s apart, 5-pt edge → actionable; 45s apart at v=0.001 → 4.55-pt adj edge,
still actionable but flagged; 120s apart → REVIEW skew-nonsimultaneous; tiny edge + moderate
skew → REVIEW skew-edge-eroded.

---

## 2. C3 — Portfolio / per-venue capital cap binding detection

**Goal:** never promote a set of actionable arbs whose combined stake exceeds bankroll or any
single venue's capital. Capital is siloed per venue (you can't instantly move USDC between
Kalshi/Polymarket/SX), so per-venue caps bind independently.

**Inputs:** `compute_exposure(store, …)` (already exists, `exposure.py`) gives committed stake
overall and per venue from currently-active bets; `config.bankroll_usd`,
`config.max_stake_pct`, `config.per_venue_caps`.

**Logic (in the scan, after detection + C1, before recording actionable arbs):**

```python
exp = compute_exposure(store, bankroll, per_venue_cap, per_venue_caps)
remaining_total = bankroll - exp.total_committed
remaining_venue = {v: caps[v] - exp.by_venue.get(v, 0) for v in caps}

# 1) collapse to one position per opinion (C2 position_key); keep the best executable venue
candidates = dedup_by_position_key(actionable_arbs)         # see ARB_WEAKNESSES C2

# 2) greedy allocation by simultaneity-adjusted edge (best risk-adjusted first)
for arb in sorted(candidates, key=lambda a: a.adj_edge, reverse=True):
    want = ladder_scaled_stake(arb.legs, bankroll, max_stake_pct, safety_frac)  # B1 sizing
    leg_venues = {leg.venue for leg in arb.legs}
    cap_alloc = min([remaining_total, want] +
                    [remaining_venue[v] for v in leg_venues])   # each leg ties up its venue
    if cap_alloc < config.arb.min_executable_usd:
        arb.status = "REVIEW"; arb.notes += "capital-capped"
        continue
    arb.stake = money(cap_alloc)
    remaining_total -= cap_alloc
    for v in leg_venues:
        remaining_venue[v] -= cap_alloc          # both legs hold their venue's capital
```

Notes:
- Each cross-venue arb ties up capital on *every* venue it touches (you fund both sides), so the
  per-venue decrement applies to each leg's venue — matching `exposure.compute_exposure`'s
  per-venue accounting.
- Greedy-by-edge is the simple, defensible rule; a later refinement is an LP that maximizes
  Σ stakeᵢ·adj_edgeᵢ subject to the total and per-venue constraints. Start greedy; unit-test the
  binding cases.
- A correlation haircut (same event, both directions) reduces combined cap further; fold in via
  the position key (one opinion = one position) before allocation.

**Config:**
```yaml
arb:
  min_executable_usd: 50
per_venue_caps:        # already present
  kalshi: ...
  polymarket: ...
  sx: ...
```

**Tests:** two arbs both wanting $80 with $100 bankroll → first funded $80, second capped to $20
(or REVIEW if < min_executable); a polymarket+sx arb when polymarket cap is exhausted →
capital-capped; verify totals never exceed bankroll or any per-venue cap.

---

## 3. C4 — Holding period charged to settlement, not event start

**Goal:** `is_economic` must compare the lock's ROI to the capital charge over the time capital
is *actually locked* — until both venues settle, not until the game starts.

**Logic:**

```python
def hold_days(legs, now, config) -> Decimal:
    finals = []
    for leg in legs:
        start = parse(leg.start_utc)
        if start is None:
            continue
        dur   = config.sport_typical_duration_hours.get(leg.sport, 4)   # game length
        lag   = config.venue_settlement_lag_hours.get(leg.venue, 24)    # venue payout lag
        finals.append(start + timedelta(hours=dur + lag))
    if not finals:
        return Decimal("0")
    return q6(Decimal(max((f - now).total_seconds() for f in finals)) / Decimal(86400))

# replace the event-start day count in arbitrage.py:170-173
days = float(hold_days(best_legs, now, config))
if not is_economic(roi, days, config.capital_charge_annual):
    notes += "uneconomic"
```

`hold_days` is the **max across legs** (capital releases only when the slowest venue settles).
Surface `$-days` (already in `exposure.duration_weighted_usd_days`) using the same horizon so
the report's time-locked exposure matches the economic gate.

**Config:**
```yaml
capital_charge_annual: 0.05
sport_typical_duration_hours: { baseball_mlb: 3.5, basketball_nba: 2.5, soccer: 2 }
venue_settlement_lag_hours:   { kalshi: 24, polymarket: 48, sx: 24 }   # verify empirically
```

**Tests:** a 0.2% lock on a game 14 days out → uneconomic; same lock same-day with 24h lag →
economic iff roi > charge(1.5 days); max-across-legs picks the slower venue.

---

## 4. D1 — Kalshi start-time fallback

**Goal:** never assign a start time days off (which corrupts event_key date + every time guard +
settlement bucket).

**Logic (`kalshi.py`, replacing the `close_time` fallback at :119-121):**

```python
start = _start_from_ticker(ticker)
if start is None:
    # do NOT fall back to close_time (it is days after the game).
    note  = "start-unknown"
    start = None                      # leave unset
# build the quote with start_utc=start and notes += note when None
```

Downstream rules for a `start-unknown` quote:
- **Not actionable.** It can appear as research/divergence but never as an actionable arb leg
  (it can't pass the started/final guard and can't be settled by start time).
- **Event key:** if `start` is None you cannot form the date-keyed `event_key`. Either skip the
  quote for arb grouping, or key it on `close_time`'s date *only* for research display, clearly
  tagged. Do not let a `close_time`-keyed quote match a real-start quote.

**Tests:** a fixture ticker with an unparseable time → quote carries `start-unknown`, `start_utc
is None`, and is excluded from `detect_true_arbs` actionable output.

---

## 5. D2 — Doubleheader leg clustering

**Goal:** stop a leg from game 1 pairing with a leg from game 2 (same teams, same day → same
event_key) and faking an arb.

**Logic (in `detect_true_arbs`, after grouping by `(event_key, market_type, line)` and before
`by_outcome`):**

```python
def cluster_by_start(quotes, tol_secs):
    # sort by start_utc; greedily split where the gap exceeds tol (e.g. 2h).
    timed = sorted([q for q in quotes if q.start_utc], key=lambda q: q.start_utc)
    clusters, cur = [], []
    for q in timed:
        if cur and (q.start_utc - cur[-1].start_utc).total_seconds() > tol_secs:
            clusters.append(cur); cur = []
        cur.append(q)
    if cur: clusters.append(cur)
    untimed = [q for q in quotes if not q.start_utc]   # research-only; own bucket
    return clusters, untimed

# run the existing per-outcome arb detection ONCE PER CLUSTER, not per event_key group.
```

- Keep the date-only `event_key` for matching (the 00:00-seam reason in `matching.py` stands);
  this clustering only prevents cross-game pairing inside detection.
- `tol_secs` = `config.arb.doubleheader_cluster_tol_secs` (e.g. 7200). The start-drift gate
  (`max_start_drift_mins`) remains a backstop for legs that survive clustering.
- Untimed legs (e.g. D1 start-unknown) never join a cluster → never actionable.

**Tests:** two MLB games same teams at 13:05 and 19:05 with cross-venue quotes → two clusters,
no cross-game arb; a true single game with legs 30s apart → one cluster, arb detected.

---

## 6. D3 — SX winning-bet fee

**Goal:** stop pricing SX legs at zero friction. SX charges its fee **only on winning bets**
(`config sx.fee_rate: 0.0` currently models singles at zero). In an arb exactly one leg wins, so
a winning SX leg *will* incur a fee that is currently ignored — overstating every polymarket+sx
arb edge.

**Fee model.** For a $1-payout contract bought at cost `c`, a win returns $1, profit `1 − c`. SX
takes `sx_win_fee_rate` of the winnings. Two ways to charge it:

- **Conservative (recommended for the actionable gate):** charge the full winning-bet fee on the
  SX leg as if it wins — `fee_cost_sx = q6(sx_win_fee_rate · (1 − c))`. This is the worst case for
  the SX leg and keeps the lock honest.
- **Expected (for reporting):** weight by win probability `≈ c` (the devigged implied prob in an
  equalized arb) — `expected_fee_sx = q6(sx_win_fee_rate · (1 − c) · c)`.

```python
# venues/sx.py, when building fee_cost for an SX leg:
win_fee = q6(sx_win_fee_rate * (Decimal("1") - cost))      # conservative
fee_cost = all_in_fee_cost(cost, Decimal("0"), buffer, basis="linear") + win_fee
notes += "sx-win-fee-modeled"
```

Add `sx.win_fee_rate` to config (verify SX's live schedule before trusting any SX arb — the
adapter already refuses live runs while the rate is unverified). Recompute `total_cost` and ROI
with the winning-leg fee included; some currently-flagged polymarket+sx arbs will correctly drop
below the threshold.

**Tests:** an arb that is +EV at zero SX fee but break-even at `win_fee_rate=0.05` must NOT flag;
conservative vs expected fee produce the documented values for `c=0.5`.

---

## Suggested build order

1. **B1/B3 first** (Polymarket multi-level `/book` ladder + the per-venue normalization tests +
   capacity UNKNOWN gate). Nothing about sizing is answerable until this lands and a live scan
   period accumulates ladder-bearing arbs.
2. **D3** (SX winning-bet fee) and **D1** (Kalshi start) — small, contained correctness fixes
   that change which arbs are even real.
3. **C1** (simultaneity haircut) and **D2** (doubleheader clustering) — detection-time guards.
4. **C4** (holding period) then **C3** (portfolio cap) — economic gating, once the per-arb
   numbers are trustworthy.

Each phase: add the unit vectors listed under it, run `pytest`, then re-run `arb arb-capacity`
and confirm the captured-ladder fraction climbs and the report stops printing
INSUFFICIENT_DEPTH_DATA.
