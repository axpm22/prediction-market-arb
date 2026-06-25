# Claude Code prompt — rebuild the dashboard UX (control center)

Paste everything below into Claude Code from the repo root.

---

You are working in the ArbitrageModel repo. **Read `CLAUDE.md` and `SPEC.md` first** — the
hard rules there override anything implied below. This is a paper-trading measurement
instrument: public reads only, no order endpoints, Decimal money math, append-only ledger,
every threshold lives in `config.yaml`. Nothing you build may place a trade or call an
authenticated endpoint.

## Goal

Replace the current dashboard with a **modern, legible, clean, to-the-point control center**
that (a) shows all the information and P&L I need at a glance, (b) lets me **turn every
strategy and every strategy *version* on and off**, (c) shows **total profit** prominently,
and (d) renders **graphs** of profit/equity over time. Today the UX is split between a
static `reports/dashboard.html` (Chart.js from a JSON dump) and a richer live server in
`arb/experimental/webserver.py` serving `/api/state` on :8787. Consolidate into one good
surface.

## What exists now (don't rediscover from scratch — read these)

- `arb/experimental/dashboard.py` — builds `reports/dashboard.json` (equity curves: optimistic/
  confirmed × raw/clean, flat and Kelly; drawdowns; EV edge buckets; segment scores). Read-only.
- `arb/experimental/webserver.py` — local `ThreadingHTTPServer` on :8787, polls `/api/state`,
  shows process health, pipeline, equity, segment effectiveness, recent bets, log tails.
- `arb/store.py` + the `flat_bets` table. Columns include: `kind, sport, live, edge,
  edge_points, stake_usd, pnl_optimistic, pnl_confirmed, pnl_kelly_optimistic,
  pnl_kelly_confirmed, status, result, placed_at, settled_at, quality_flag, validation_rule,
  validation_tag, notes`. Ledger `kind` values: `ARB, EV, DIVERGENCE, CLOSE, NEGRISK` (+ the
  experimental strategies below). It is append-only — read it, never mutate it.
- `config.yaml` — the single source of truth for every toggle (see "Versions" below).
- Worker processes (each a `python -m ...` entrypoint, listed in `webserver.py` `PROCESSES`/
  `LOG_FILES`): `daemon, ev-exit, negrisk, political, nonsports, closing, btc, whales`, plus
  `focus, verify, hot` watchers and the `live_baseball_ev` lane.

## Hard constraints (do not violate)

1. **Read-only data plane.** All P&L/quote/bet reads come straight from the WAL-backed
   SQLite ledger. Never write to `flat_bets` or any ledger table from the UI.
2. **Paper-only.** The control plane may toggle config and start/stop *measurement* workers,
   but must never enable order placement or any authenticated endpoint. There is no "go live"
   button. Keep `consensus_enabled`/`betting_enabled` semantics exactly as the config defines
   them; surfacing them is fine, but flipping a betting flag must show a clear "still paper /
   gated" status and must not wire any order path.
3. **Config is the source of truth (CLAUDE.md rule #4).** Toggle writes go to `config.yaml`
   (or a clearly-namespaced `config.local.yaml` overlay that the loader already merges — check
   `arb/config.py` and use the existing mechanism; if none exists, write `config.yaml` with a
   safe atomic write + timestamped backup). No magic numbers baked into the UI.
4. **No secrets in any served file or JSON.** The `ODDS_API_KEY` and `.env` never appear in
   the page, the API, or `dashboard.json`.
5. Keep money math in `Decimal` server-side; format for display only.

## The control plane (turn everything on/off)

Add a control surface to the local server (extend `webserver.py`, or refactor it into a
small `arb/experimental/control.py` it imports — your call, keep modules <300 lines):

- **Strategy/worker toggles:** for each worker, show running/stopped/health and an on/off
  control that starts or stops that `python -m ...` subprocess (reuse the process-detection
  already in `webserver.py`). Persist the desired on/off state so a toggle survives restarts.
- **Config-flag toggles:** surface every `enabled:`-style flag and strategy *version* selector
  from `config.yaml` (enumerated below) as labeled switches/dropdowns. Writing one updates
  `config.yaml` atomically (backup first) and tells me which workers need a restart to pick it
  up.
- **Guardrails:** any toggle that touches a betting/enable flag renders with a distinct color
  and an explicit "measurement-only — no orders" confirmation. POSTs are same-origin only and
  the server stays bound to localhost.

## "All versions of each strategy" — enumerate and make each switchable

Treat a *version* as a selectable variant of a strategy, driven by config. At minimum surface
and toggle these (read `config.yaml` for exact keys; add any I missed):

- **Consensus de-vig:** `devig_method: proportional | power | shin` (+ show the regrade
  disagreement if available).
- **Sharp-book-weighted consensus (EV#2):** opt-in flag — show on/off and which books count
  as sharp (`consensus.sharp_books`).
- **EV uncertainty (EV#3):** dispersion gate + lower-bound Kelly on/off.
- **EV FLB calibration (EV#5):** isotonic p̂ adjustment on/off.
- **EV stale-consensus decay (EV#7):** continuous vs binary.
- **Slippage model (does-well#2):** `slippage_model: flat | scaled` — flag this as a
  cost-vector-changing version (versioned, not silent).
- **Micro-EV lane**, **EV-exit + stop-loss**, **LIVE_BASEBALL_EV lane**, **exposure caps
  (per-venue + duration-weighted)**, **parlay**, plus each experimental sweep (negrisk,
  political, nonsports, btc, whales, closing).

For each version show: on/off, a one-line description of what it changes, and — where the
ledger supports it — its **own P&L line** so I can compare versions head-to-head.

## Total profit + graphs (the headline)

- **Total profit** as the top hero number, with the four honest variants selectable:
  optimistic vs confirmed × raw vs clean, and flat-$1 vs quarter-Kelly. Default the headline
  to **confirmed / clean / flat-$1** (the project's honest baseline) and label it as such.
- **Equity-over-time graph** (cumulative P&L by `placed_at`/`settled_at`), with a series
  toggle for each variant above and a **per-strategy / per-version breakdown** (stacked or
  multi-line). Include **drawdown** and **profit attribution by strategy** (which `kind`
  made/lost money).
- Secondary charts: EV edge buckets (raw vs clean), segment scores/tiers, optimistic−confirmed
  "mirage gap", and bet counts. Keep Chart.js (already a CDN dep) unless you have a strong
  reason; do not add a build step.

## UX quality bar

Clean, legible, information-dense but not cluttered: a clear visual hierarchy (hero total →
strategy cards → charts → recent bets/log tails), responsive single-page layout, accessible
contrast and font sizes, keyboard-reachable toggles, and a manual refresh plus the existing
auto-poll. Show data freshness (last-generated timestamp) and empty/loading states. No dead
space; group related controls. Match the repo's existing dark dashboard aesthetic but improve
typography and spacing.

## Deliverables

1. The rebuilt page (extend `reports/dashboard.html` and/or the server-rendered page in
   `webserver.py` — one canonical surface; if you keep both the static export and the live
   server, they must share the same data shape from `dashboard.py`).
2. Any new `/api/...` endpoints for state + toggles, documented at the top of the file.
3. Updated `dashboard.py` JSON shape if you add per-version P&L series (additive, don't break
   existing keys).
4. Tests: extend the existing dashboard/webserver tests (`tests/test_dashboard.py`) — assert
   the new endpoints return well-formed JSON, toggles write config + back it up and never
   touch ledger tables, and no secret strings appear in any served payload.

## Acceptance / verification (do this before declaring done)

- `pytest` green, including new tests.
- Start the server, load the page, and **take a screenshot** of: the hero total, the strategy/
  version toggle panel, and the equity graph with multiple series on. Confirm legibility.
- Flip a config-flag toggle; show the `config.yaml` diff + that a backup was written and the
  ledger was untouched (`git diff` shows only config + UI files).
- Confirm `grep -ri "ODDS_API_KEY\|api_key" reports/ ` and the served JSON contain no secret.

## Out of scope / do not

- No order placement, no authenticated calls, no "live trading" mode, no new frameworks/build
  step, no edits to the append-only ledger, no new always-on network exposure (localhost only).

Plan the information architecture and the control-plane design first, show me the plan, then
implement. Keep changes reviewable and modules under ~300 lines.
