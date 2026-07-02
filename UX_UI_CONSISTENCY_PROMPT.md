# Claude Code prompt — unify the UX/UI (one language, one set of numbers)

Paste everything below into Claude Code from the repo root.

---

You are working in the ArbitrageModel repo. **Read `CLAUDE.md` and `SPEC.md` first** — the
hard rules there override anything below. Paper trading only, read-only data plane,
append-only `flat_bets`, Decimal money math, every threshold in `config.yaml`, no Odds API
credits in tests.

## Goal

Make every user-facing surface **agree with every other surface**, and simplify while doing
it. Same numbers, same names, same colors, same ordering — everywhere. A user glancing at
the CLI report, the static dashboard, and the live control center should never see two
different figures, labels, or groupings for the same thing.

## The surfaces (read all three before changing anything)

1. **CLI** — `arb/report.py` (rich tables: report, positions, opportunities) plus scattered
   `console.print` in `arb/cli.py` and `arb/tracker.py`.
2. **Static dashboard** — `arb/experimental/dashboard.py` → `reports/dashboard.json` →
   `reports/dashboard.html` (Chart.js).
3. **Live control center** — `arb/experimental/webserver.py` on :8787 (`/api/state`,
   embedded HTML/CSS/JS, ~2,750 lines).

## What "agree" means (audit, then fix)

1. **One number per fact.** Total P&L, per-lane P&L, ROI, win rate, drawdown, bet counts
   must come from ONE computation path. Extract the shared aggregation into a single module
   (e.g. `arb/metrics.py`, computed from raw `flat_bets` rows) and make all three surfaces
   consume it. No surface may re-derive P&L with its own SQL or JS arithmetic. If two
   surfaces currently disagree, the recompute-from-raw-log path wins.
2. **One vocabulary.** Fix one canonical label per concept and use it everywhere:
   lane/kind names (`ARB, EV, DIVERGENCE, CLOSE, NEGRISK`, experimental lanes), the
   optimistic/confirmed × raw/clean × flat/Kelly axes, status values (`FILLED, REVIEW,
   SETTLED…`). Put the label map in one place (a small dict in the metrics module) —
   the CLI, JSON builder, and webserver all read it. Kill synonyms ("track" vs "lane"
   vs "strategy", "profit" vs "P&L" vs "pnl" in display text — pick one each).
3. **One visual language.** Same semantics for color (green = positive/confirmed-good,
   red = negative, amber = REVIEW/unverified), same money and percent formatting (one
   `fmt_money` / `fmt_pct`, Decimal in, string out), same lane ordering (fixed canonical
   order, not insertion order) in every table, card grid, and chart legend.
4. **One source of truth for toggles.** Anything the UI shows as on/off must reflect
   `config.yaml` — never a hardcoded list drifting from it.

## Simplify (apply while unifying — don't gold-plate)

- `webserver.py` is ~2,750 lines against the ~300-line module rule. Split it: server/API,
  state collection, and the frontend (move the embedded HTML/CSS/JS to a template file or
  a small static dir served by the same server). Cut any panel that duplicates what
  another panel or surface already shows better — fewer sections, each earning its place.
- If the static `dashboard.html` is now fully redundant with the live control center,
  reduce `dashboard.py` to just the JSON export (backtest artifact) and say so — or keep
  it as a no-server fallback, but it must render from the SAME shared metrics, not its
  own math.
- Prefer deleting a divergent display over adding a config option to reconcile it.

## Non-negotiables

- Read-only: no writes to `flat_bets` or any ledger table. No new dependencies.
- All existing tests keep passing (`pytest`), especially `tests/test_dashboard.py` and
  `tests/test_tracker.py` — update tests only where labels/paths legitimately changed.
- Add one test that locks the agreement in: feed a small fixture of `flat_bets` rows to
  the shared metrics module and assert the CLI report, `dashboard.json`, and `/api/state`
  all emit identical totals per lane.

## Deliverables

1. The refactor (shared metrics module + label map, three surfaces consuming it,
   webserver split under ~300 lines/module).
2. The agreement test above, passing.
3. A short `UX_UI_CHANGES.md`: what was renamed (old → new), what was deleted and why,
   and any number that changed value because a surface had been computing it wrong.

Work in phases: (1) audit + write the discrepancy list, (2) shared metrics/labels module
with tests, (3) migrate CLI, (4) migrate JSON/static, (5) migrate webserver + split files,
(6) run `pytest` and show output. Show me the discrepancy list from phase 1 before you
start rewriting surfaces.
