# attic — parked, not deleted

Code that is on no live path. Moved here 2026-07-28 after a reference sweep
found zero call sites anywhere in `arb/` or `tests/`.

Parked rather than deleted because most of it is a probe we will want back the
moment a lane that needs it switches on. Git has the history either way; this
directory exists so the live tree shows only what runs.

`pytest` does not collect this directory (the test file is deliberately named
`unused_tests_*.py`, which does not match `test_*.py`).

| item | was in | why it is here | restore when |
|---|---|---|---|
| `exit_policy_pnls` | `arb/metrics.py` | never called, and returns immediately unless the bet has exactly ONE leg — so it has never evaluated a locked arb. Measured 2026-07-28: unwinding a 2-leg cover costs a median 9c of round-trip spread against a 0.5c edge, and a covering pair is delta-neutral, so waiting for a better exit cannot help. | the single-leg EV lane re-opens |
| `fetch_event` | `arb/venues/kalshi.py` | was for negRisk mutually-exclusive checks, but `arb/negrisk.py` is pure math over dicts and imports nothing from the venue | negRisk needs live ME verification |
| `fetch_trades` | `arb/venues/kalshi.py` | tape probe; the bench pulls tape through `arb/kalshi_history.pull_trades` instead | `tape_check` is wired up |
| `refresh_series_cache` | `arb/venues/kalshi.py` | series list is static in `SPORT_SERIES` | coverage expands past a hand-maintained list |
| `fetch_market_status` | `arb/venues/sx.py` | SX settled-status semantics are unverified (`data/verified_facts.md`), so `reconcile._sx` returns None unconditionally and never calls it | SX settlement is verified live |
| `fetch_scores` | `arb/venues/oddsapi.py` | settlement resolves MLB through the free MLB Stats API, so this would spend Odds API credits for data we already get for nothing | a sport without a free score feed is added |

## Left in place on purpose

`arb/settlement_matrix.py` (`load_matrix`, `sport_all_green`) is also uncalled,
but it is **not** dead weight — it is the §18.2 trusted gate. `arb/eligibility.py:87`
gates trust on `cfg["trusted_pairs"]` directly and never consults the matrix, so
the gate exists and nothing asks it. That is a wiring gap to close, not code to
file away. Parking it would quietly remove a safety mechanism.

## Also now unused

`config.yaml`'s `exit:` block feeds only `exit_policy_pnls`. Left in place
because the loader validates it and removing keys is a breaking change for no
gain. Delete it whenever the exit engine is retired for good.
