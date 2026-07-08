# ArbitrageModel (v3.5 rebuild)

A Kalshi / Polymarket / SX Bet **paper-trading arbitrage measurement
instrument**. It watches the same event priced on multiple venues, flags
opportunity classes (same-venue `ARB`, cross-venue `XSPREAD`, `NEGRISK`
baskets, `POLITICAL` intra-market, `+EV` vs de-vigged consensus — shipped
frozen), and maintains an honest, append-only flat-$1 paper ledger answering
one question: *"How much would I have made by today if I'd bet $1 on every
flag?"*

**It never places orders.** Its most valuable outputs so far have been honest
nulls. Read `SPEC.md` (the full v3.5 spec), `CLAUDE.md` (the 13 hard rules),
and `PREREGISTRATION.md` (frozen hypotheses and gates) before touching
anything.

## Setup

```bash
python3.13 -m venv .venv && source .venv/bin/activate   # 3.11+ works
pip install -r requirements.txt
cp .env.example .env      # optional: add ODDS_API_KEY (free, the-odds-api.com,
                          # 500 credits/mo) — consensus/EV machinery no-ops
                          # gracefully without it
pytest -q                 # must be green — rule 13: nothing runs otherwise
python -m arb report      # empty frame + DB creation sanity check
```

No key is required for the three venues (all public keyless GETs) or the MLB
Stats settlement oracle. The Odds API key powers `arb scan` (consensus) and
non-MLB score settlement only. When you add one, record a real fixture on the
first `arb scan` and replace the synthetic `tests/fixtures/oddsapi_h2h.json`
(see `data/verified_facts.md`).

## Run

```bash
caffeinate -i python -m arb supervisor    # macOS; the ONLY sanctioned way to run loops
python -m arb status                      # heartbeats, quote age, backups, open bets
cat reports/daily/$(date +%F).md          # the product — written 23:59 America/New_York
```

- `caffeinate -i` does not survive lid-close or battery — on a laptop set
  `sudo pmset -b sleep 0 disablesleep 1` or run headless/plugged-in. Better:
  install a launchd/systemd unit with `KeepAlive` pointing at
  `python -m arb supervisor`.
- The supervisor owns all loops (one-shot `watch --once` children, relaunched
  each `watch_interval_secs`) and the clock (`scan_times`, `settle_time`,
  `daily_report_time`, daily DB backup, weekly shape-check, monthly canary).
- A second supervisor refuses to double-run (`.locks/`).

**Stop / restart:**

```bash
touch data/stop-all       # halts every loop between cycles (in-flight cycles finish)
# restart procedure (rule 13): commit, pytest -q green, then:
rm data/stop-all && caffeinate -i python -m arb supervisor
```

A fix that is not committed does not exist — children stamp `head_sha` into
their heartbeats so a stale process is visible in `arb status` instantly.

## Commands

| Command | Purpose |
|---|---|
| `watch [--once] [--dry-run]` | primary loop: fetch → match → detect → track → persist |
| `scan` | Odds API consensus refresh (no-op if disabled / no key) |
| `settle [--manual EVENT RESULT -y]` | resolve pending bets; auto-runs reconcile + check-invariants |
| `report [--daily DATE]` | recompute any day idempotently from raw rows |
| `status` | supervisor state, data freshness alarm, backup age, open bets |
| `check-invariants` | the six ledger invariants; silent when clean |
| `negrisk-scan` | Polymarket negRisk basket scan (both sides, exhaustiveness proof) |
| `political` | Polymarket-only intra-market YES+NO<1 lane |
| `verify` | 10s flash re-quote of fresh locked flags (vanish rate + paper fill oracle) |
| `dashboard` | read-only instrument panel on `http://127.0.0.1:8722` (config `dashboard.*`) |
| `canary` | synthetic fixture through the full pipeline; P&L must match to the cent |
| `shape-check` | live response schema vs recorded fixtures |
| `backup` | `VACUUM INTO` a dated snapshot in `backups/` |

## Reading the daily report

`reports/daily/YYYY-MM-DD.md` (committed to git — versioned evidence; CSVs
stay ignored):

- **Headline** = trusted + sports + pregame + clean lane only, optimistic
  basis, **gross of carry**. Everything else (review lane, non-sports,
  micro/live/AI-matched, XSPREAD) prints in its own section, never blended.
- **Missed confirmations count $0** in the confirmed column next to the miss
  rate — never silently dropped (the V1 survivorship artifact).
- **Left on the table** shows every filtered flag by reason — the filters'
  cost is measured, not assumed.
- **Cadence line**: planned vs actual poll interval (p95 is the loop's real
  spec; V1 believed 60s while running ~140s).
- A restated day (pending bets settled later) is marked `*`.

## What to expect

Weeks one–two: plumbing checks and **zero clean settled evidence — that is
correct behavior**. Cross-venue XSPREADs are REVIEW-only forever by default;
the EV lane is frozen; the first real deliverables are the honesty metrics
(flagged vs realized edge, cadence, left-on-table) and the negRisk / ladder
totals verdicts at their pre-registered n (see `PREREGISTRATION.md`). If
everything nulls out, the instrument has done its job at the cost of
electricity.

## Layout

See `SPEC.md` §5. One eligibility predicate (`arb/eligibility.py`), one metrics
module (`arb/metrics.py`), one fee implementation (`arb/venues/fees.py`),
append-only ledger (`flat_bets` + `bet_events`) — all enforced by CI grep-tests
in `tests/test_hygiene.py`. `scripts/headline.sql` independently recomputes the
headline number; CI asserts it matches `metrics.py`.
