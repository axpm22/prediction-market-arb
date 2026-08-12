# prediction-market-arb

A paper-only research system that hunts cross-venue arbitrage in prediction markets —
Kalshi, Polymarket, and SX Bet — and keeps a brutally honest, append-only record of
whether the edges it finds were ever real. No orders are placed; live code uses only
public, unauthenticated GET endpoints (plus a The Odds API key for optional sportsbook
consensus). The core question it answers: **if I had put $1 on every flag, where would
I be today — and how much of the claimed edge survived contact with reality?**

**Start here:** [RESULTS.md](RESULTS.md) (the findings) ·
[PREREGISTRATION.md](PREREGISTRATION.md) (frozen methodology) ·
[INTERVIEW_NOTES.md](INTERVIEW_NOTES.md) (the short version). Headline finding: cross-venue
sports arbitrage nets **≈$0 on confirmed fills** — the mirage the harness was built to
measure honestly.

## The strategy

### 1. Prices are probabilities, and the same bet trades in three stores

Every market we watch sells the same primitive: a contract paying $1 if an outcome
happens. Its price *is* the market's implied probability, which makes prices directly
comparable across venues once everything is expressed in one unit: **effective cost per
$1 of payout** — the venue's real ask (from the order book, never a displayed midpoint)
plus that venue's fees. Kalshi, Polymarket, and SX are independent pools of bettors
pricing identical events, and independent pools occasionally disagree.

### 2. The bet: true arbitrage, locked at entry

When the cheapest asks covering *every* outcome of one market sum below $1 — Rangers
63.65¢ all-in on Kalshi, Royals 36¢ on Polymarket, total 99.65¢ — buying all sides
returns $1 no matter who wins. Profit is locked at purchase: `ROI = 1/S − 1` where S is
the summed cost. No opinion about the game is involved. These windows exist because
venues reprice at different speeds for seconds at a time, and they are small (real ones
run 0.2–1%) because everyone hunting them eats the large ones instantly. Anything above
~4% is auto-flagged as a probable stale quote or settlement trap rather than celebrated.

Two weaker signals are logged but not bet: **divergence** (same outcome priced ≥3 points
apart without locking profit — research fuel, recorded at $0 stake) and **+EV**
(venue price cheaper than the de-vigged median of ≥3 sportsbooks — positive expectation,
zero guarantee; disabled by `consensus_enabled: false`, with an optional closing-line
recorder that snapshots consensus exactly once per game, minutes before start).

### 3. Finding the same bet: matching with paranoia

Venues name things differently ("Texas" / "Texas Rangers" / "TEX"), so quotes are joined
by normalized aliases per sport, date, and a 6-hour start bucket (so MLB doubleheaders
never blend). Totals match only on the **exact** line — Over 8.5 and Over 9.0 are
different bets. Two sanity gates suspend suspect matches before they can flag: venues
must agree on start time within 30 minutes, and the same outcome priced >15 points apart
across venues is treated as a matching error, not money. Non-sports events (politics,
econ, weather) are never auto-matched at all: a human promotes fuzzy-suggested candidates
into a manual map after reading both venues' resolution wording.

### 4. The evidence ladder: a flag is a claim, not a fact

Flagged edges die three ways — they were never simultaneous, they were stale, or the
venues settle differently. Each has a dedicated defense, and every bet carries its grade:

| Grade | Mechanism | Kills |
|---|---|---|
| depth floor | quotes under $50 of book depth never enter detection | dust orders posing as prices |
| time-skew label | legs captured >60s apart are flagged REVIEW | cross-venue ghosts from slow sweeps |
| flash verify (~10s) | the exact legs are re-quoted; VERIFIED or VANISHED, with **atomic** and **sequential** edges (leg one held at detection price) | stale quotes; measures leg risk |
| focus confirmation | a bet-by-bet rotation re-quotes one game's books on all venues within seconds | non-simultaneity, by construction |
| trusted_pairs | cross-venue flags stay REVIEW until both rulebooks are read and the pair is whitelisted | settlement mismatches — the silent killer no re-quote can detect |

### 5. The ledger: append-only or it didn't happen

Every flag becomes a $1 paper bet in `flat_bets`. Rows are never edited or deleted —
confirmations, closes, settlements, and corrections are all *new* rows, and known-bad
bets (e.g. from a fixed bug) are excluded only via an immutable annotations table that
requires a written reason. Each bet records both an **optimistic** P&L (first-sighting
prices) and a **confirmed** P&L (second-sighting prices); the gap between them is the
measured cost of being slower than the market. Every quote ever observed (100k+ and
counting) is persisted as backtest data. Settlement runs daily against final scores;
ARBs are settled at entry by definition. Daily reports are recomputed from raw rows.

Scope, stated plainly: this ledger measures whether flagged prices were real and
simultaneous. It does not measure capture — execution latency and partial fills are
simulated (sequential edge), and capital constraints are excluded.

## Architecture: four cooperating processes, one ledger

| Process | Cadence | Role |
|---|---|---|
| `python -m arb daemon` | sweep ~4.5 min | discover markets, persist every quote, wide detection, daily settle + report |
| `python -m arb.experimental focus` | rotation ~1–2 min | same bet, all books, at one time — simultaneous detection + confirmations |
| `python -m arb.experimental verify` | poll 10s | flash VERIFIED/VANISHED verdicts on fresh arbs |
| `python -m arb.experimental closing` | check 60s | one closing-line EV snapshot per game (off while EV is disabled) |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # ODDS_API_KEY only needed for consensus/EV and score settlement
```

All venues are keyless. Kalshi needs `data/kalshi_series.json` (checked in; re-probe when
a sport shows 0 quotes for a day — season changes move series). SX runs with
`sx.fee_rate: 0.0`, confirmed from docs.sx.bet; the adapter refuses live runs if it is
ever set back to null.

## Safe local checks (no live calls, no credits)

```bash
pytest
python -m arb report
python -m arb watch --once --fixtures --dry-run --sport baseball_mlb
python -m arb scan --fixtures --dry-run --sport baseball_mlb
```

## Running

```bash
# the full loop, detached, sleep-resistant:
nohup env PYTHONUNBUFFERED=1 caffeinate -i .venv/bin/python -m arb daemon >> logs/daemon.log 2>&1 &
nohup env PYTHONUNBUFFERED=1 .venv/bin/python -m arb.experimental focus  >> logs/focus.log  2>&1 &
nohup env PYTHONUNBUFFERED=1 .venv/bin/python -m arb.experimental verify >> logs/verify.log 2>&1 &

python -m arb report                  # ledger totals
python -m arb report --daily          # the $1-per-flag daily answer
python -m arb settle                  # resolve open bets against final scores now
python -m arb.experimental annotate ID TAG R…  # immutable bet annotation (reason mandatory)
python -m arb.experimental nonsports-suggest   # fuzzy candidates for the manual non-sports map
pkill -f "arb daemon"                 # stop (same pattern for arb.experimental processes)
```

Daily files land in `reports/daily/YYYY-MM-DD.csv` and `.md`. Credit budget when EV is
on: scans + settlement ≈ 450/month against the 500 free tier, with a hard abort below 50.

## Reading the output

`TRUE ARB` — outcome-covering cost < $1, profit locked *if* filled and *if* settlement
matches; REVIEW means at least one of those ifs is unproven. `DIVERGENCE` — logged
spread, no stake. `+EV` — consensus-relative value, only when enabled. The honest
numbers are in the verdict trail: flagged → flash-verified → focus-confirmed → settled,
and the daily report's flagged-edge vs realized-P&L line is the entire project in one
row.
