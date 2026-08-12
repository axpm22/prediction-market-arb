# Improving the Micro-EV lane and the Bitcoin lane (June 2026)

Both lanes are enabled and running but have produced **zero** recorded bets: `flat_bets`
has 0 rows with `quality_flag='micro'` and 0 with `kind='ARB_BTC'`, and the last BTC polls
processed 562 quotes → 0 arbs. So the work here is mostly "why is it empty," not "tune the
edge." Grounded in `arb/cli.py` (micro), `arb/btc_arb.py`, `arb/experimental/btc_sweep.py`,
and the `micro:` / `btc_arb:` config blocks.

---

## Part 1 — Micro-EV lane

The micro lane (`detect_micro_ev_from_consensus` → `_mark_micro_ev`, `split_depth_lanes`,
`micro:` config) prices quotes whose depth is in `[micro.min_depth_usd=5, min_executable_usd=50)`
against consensus, with a stricter `e_min=0.08` and a depth-capped stake ≤ `max_stake_usd=25`.

### Why it's empty

**M0. It inherits the consensus-coverage starvation.** Micro EV calls the *same*
`detect_ev_from_consensus` against the *same* consensus, then filters harder. With consensus
covering only ~9 live MLB events (13.5% of venue quotes match — see the earlier EV coverage
finding), and a stricter `e_min`, almost nothing survives. **The single biggest unlock for micro
is the consensus-coverage work (World Cup / season-aware `consensus_sports`), not micro-specific
tuning.** Fix coverage first; micro rides on it.

### Improvements (after coverage)

**M1. Route unknown-depth thin venues into micro instead of silently into the normal lane.**
`split_depth_lanes` puts every `depth_usd is None` quote in the *normal* lane. SX (and sometimes
Polymarket) don't report depth, so genuinely thin books are priced as if executable at full size
and never reach the micro lane. Add a per-venue "assume-thin-when-depth-unknown" set so those
venues' unknown-depth quotes go to micro at a conservative capped stake, rather than overstating
executability in the main lane.

**M2. Price the micro stake by walking the ladder, not at top-of-book.** `_mark_micro_ev` caps
the *stake* to visible depth but still uses the quote's top-of-book `effective_cost` as the fill
price. On a $5–$50 book a $25 stake walks through levels. Use the existing `walk_ladder` /
`depth_scaled_buffer` (already in `pricing.py`) to compute the volume-weighted fill cost **for the
intended micro stake**, and require the edge to survive *that* cost. Today a micro "edge" can be an
artifact of pricing $25 at the $1-of-depth top tick.

**M3. Make the micro buffer depth-scaled, not the flat taker buffer.** Micro edges are small, so
friction is first-order. Add a micro-specific slippage buffer that scales with `stake/depth`
(floored at the latency buffer), so a 9-point "edge" on a $6 book isn't booked at the same cost
basis as a deep book. This is the depth-scaled-slippage idea (EV#6/does-well#2) applied to the
micro stake specifically.

**M4. Cap micro exposure per event and per venue.** Many micro quotes are correlated thin books
on the same game. Add a micro capacity cap (sum of micro stakes per `event_key` and per venue ≤ a
configured ceiling) so the lane can't book ten $25 bets on one opinion across thin books.

**M5. Validate micro before trusting it — thin books are where fake edges live.** Thin/illiquid
books are exactly where stale or mis-scaled prices produce phantom edge. Grade micro on its **own**
CLV and calibration (the `clv`/`segment_calibration` machinery already exists) as a separate
segment, and keep micro quarantined (`quality_flag='micro'`, never headline) until its CLV CI low
is `> 0`. Right now it would book on detection alone.

**M6. Tie `e_min` to depth.** A flat `0.08` is arbitrary across the $5–$50 band. Make the micro
edge floor a function of depth (more edge required as depth → $5), so the threshold reflects that
thinner = riskier = needs more cushion. Keep `0.08` as the floor at the top of the band.

---

## Part 2 — Bitcoin lane

`find_btc_arb` pairs the cheapest cross-venue `UP+DOWN` on a matched `(asset, window_end)`, where
`asset = BTC@strike@YYYYMMDDHHMM`. Every flag is `REVIEW` because the oracles differ (Kalshi =
CF Benchmarks BRTI, Polymarket = UMA), per hard rule #6.

### Why it's empty (562 quotes → 0 arbs)

**The cross-venue match is structurally near-impossible.** It requires the *exact same strike* AND
the *exact same window-end minute* between Kalshi hourly threshold markets (`KXBTCD`, "BTC ≥ $X at
this hour's close") and Polymarket's differently-structured BTC markets. The two venues rarely list
the same strike at the same resolution minute, so the `asset` key almost never collides → 0 pairs.
And even on the rare match, oracle mismatch forces REVIEW — never a fillable fill.

### Improvements (ordered by value)

**B1 — Add the single-venue intramarket BTC arb (biggest, and it's *fillable*).** `find_btc_arb`
explicitly does `if up.venue == down.venue: continue  # cross-venue only`. But the cleanest BTC
edge is *within one venue*: on the same market, `yes_ask + no_ask < 1` (after fees) is a guaranteed
$1 payout — **same oracle, same window, no rulebook risk, status OK not REVIEW**. This is the
`political`/`negrisk` intramarket pattern (already trusted elsewhere) applied to BTC. Add a
same-venue YES+NO detector; it's the only BTC path that can ever be filled. Extend to the Kalshi
threshold *ladder* too: `P(BTC≥$X)` must be ≥ `P(BTC≥$X+1)`; buy `NO(≥X)` + `YES(≥X+1)` when the
ladder is internally inconsistent.

**B2 — Fix the Kalshi fee basis (concrete bug).** `parse_kalshi_btc` prices legs with
`all_in_fee_cost(yes_price, fee_rate, taker_buffer)` — which **defaults to `basis="linear"`** — but
Kalshi's fee is the quadratic `p(1−p)` curve, and `arb/venues/kalshi.py` correctly uses
`basis="quadratic"`. So every Kalshi BTC leg is mis-priced (over-charged near the middle,
under-charged at the tails), shifting every BTC `total_cost`/edge. Pass `basis="quadratic"` for the
two Kalshi legs (Polymarket stays `linear`). This is the same class of bug the sports pipeline
already fixed.

**B3 — Build the oracle-divergence tracker (the path from REVIEW → fillable).** The cross-venue
lane is permanently REVIEW because BRTI vs UMA aren't *proven* identical. Make that an empirical
question: at each window end, record both venues' settlement print and the realized outcome; over N
windows, measure how often BRTI and UMA disagree on the binary result (and by how much in price).
If they never diverge beyond a tick, you have the evidence to whitelist the `(kalshi,polymarket)` +
`btc` pair in `config.yaml` (the existing trusted-pair mechanism) and promote those flags to OK.
Without this, cross-venue BTC can only ever be measurement.

**B4 — Align resolution timestamps to the true settlement instant.** The `asset` key folds the
window to *minute* granularity from Kalshi `close_time` and Polymarket `endDate`, which use
different conventions. Match on the actual settlement instant within `window_match_secs` (=5, the
exact-window rule) rather than a string minute key, so genuinely simultaneous windows aren't missed
by a 1-minute representational gap. Keep strike matching exact (a different strike is a different
event — never interpolate).

**B5 — Widen the universe so coincidences exist at all.** Cross-venue BTC arb only appears when a
strike+window coincides. Add ETH and more Kalshi BTC series / more Polymarket BTC windows so there
are more chances for a genuine same-strike/same-window pair. Pure measurement upside (still REVIEW
until B3), and it costs no Odds-API credits (these are the keyless venues).

**B6 — Harden strike/leg parsing.** `_strike` drops a leg whenever the `$`/`T` regex misses
(e.g., a Kalshi ticker format change or a Polymarket question phrasing). Log parse-miss counts so a
silent format drift doesn't quietly empty the lane, and add the observed formats to fixtures.

---

## Suggested order

1. **Consensus coverage** (World Cup / season-aware sports) — unlocks micro *and* main EV. Do first.
2. **B2 Kalshi BTC fee basis** — tiny, concrete correctness fix; do immediately.
3. **B1 single-venue intramarket BTC arb** — the only *fillable* BTC edge; highest value in the lane.
4. **M2/M3 ladder-priced micro fills + depth-scaled buffer** — stops phantom micro edges.
5. **M5/B3 validation + oracle tracker** — required before either lane is ever trusted to bet.
6. **M1/M4/M6, B4/B5/B6** — coverage and robustness once the above land.

All of this stays paper-only; the single-venue BTC arb and the fee fix are the two changes that
both increase *real* opportunity and improve correctness. Want me to turn this into a Claude Code
prompt (like the World Cup one), starting with B2 + B1?
