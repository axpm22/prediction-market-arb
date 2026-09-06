# L14 whale lane — three-way audit (2026-09-06)

Three independent read-only passes over the tape (`data/whale_tape.sqlite`, sha256
`b0179260…153658`, 1,704,996 prints / 242,561 signals / 121,804 positions_mat / 119,482
resolutions / 4,007 wallets) and the code at `rebuild-v3` `6d92dac`: an instrument map, a
backtest, and a test/hard-rule audit. Nothing here changes the lane; anything it suggests
for a registered variant goes through PREREGISTRATION.md as a dated amendment with the
operator's name on it. Engineering defects (§4) were fixed the same day under the §16
workflow and are cited by commit.

The ledger `data/arb.sqlite` was recreated empty on this server at 2026-09-06T20:36:30Z, so
every settled-ROI / markout / exit-vs-hold number in `reports/whale-status-2026-09-06.md`
is **not reproducible here**. Everything below is computed from the tape alone.

## 1. The registered primary cannot be measured, and its scheduled calibration inverts it

V2 is the pre-registered primary; all four §12 kill rules are computed on `V2/flat`
(`arb/whale_kill.py:143`). On the tape V2 has fired **0 of 42,560** evaluations
(34,918 suppressed `uncalibrated`, 7,642 `ratio`). `signals.bet_ids_json` is non-null on
**0 of 242,561** rows. `arb bench` L14 correctly reports `no settled V2/flat booking yet`.
`whale.kill.decision_n = 100`, actual n = 0.

The day-7 calibration scheduled for **2026-09-07** sets `min_points.V2 = 3 × q10` of
per-wallet median excess (`arb/whale_signal.py:284 calibrate_min_points`). Computed two
independent ways (offline replay of `point_of`/`tally` over `signals.whale_ids_json`, and
`arb whale-score --calibrate`), both give:

    qualifying wallets 205 · q10 = 0.000663 · V2 = 0.001989 · V3 = 0.002046

Per-wallet median-excess deciles: 0.00028 / 0.00066 / 0.00066 / 0.0036 / 0.0069 / 0.0098 /
0.011 / 0.012 / 0.020 / 0.041. At `min_points = 0.001989`, **93.6% (32,669 / 34,918) of V2
rows that already cleared the 0.80 ratio gate also clear min-points** — a single
median-scoring whale, unopposed, fires the primary. The registered hypothesis text is that
*several* whales independently take the same side. Upper-bound counterfactual ≈32,000 V2
fires over the 3-day tape; applying V6's measured 91% rejection at the 300 s print-age gate
gives ≈2,900, against V1's 67 lifetime fires.

**Proposed amendment text (operator's decision, not applied):** "V2/V3 calibration on
2026-09-07 is HELD. The registered formula `3 × q10` yields 0.001989, at which one median
whale fires; that contradicts the multi-whale hypothesis the variant was registered to
test. Either (a) V2 stays uncalibrated and the primary is declared uncomputable at the
observed fire rate, or (b) the threshold is re-registered with an explicit floor of the
2nd-whale contribution (points of a lone median whale must NOT clear it), dated, before
any V2 booking is scored."

## 2. V5 has been V1 with a looser threshold for most of its fires

`arb/whale_elite.py:140-155 _warn_degenerate` exists for exactly this: cutoff ≤
`min_score_lb` ⇒ "V5 is a COPY of V1". On the tape `meta.elite_degenerate_day =
2026-09-06`; of 10,659 `elite_cutoffs` rows only **664** sit above the 0.0 bar (538 on
09-04, 126 on 09-05, **0 on 09-06**). `tennis:WINNER` went `+0.013174 (n=210)` →
`−0.033598 (n=1436)`. **47 of V5's 55 lifetime fires happened on 2026-09-06**, i.e. under
a cutoff below the qualification bar, so for them V5 ≡ V1 with `min_points` 2 instead of 3.
Drivers already documented separately (KNOWN_ISSUES #37 band-adjust switch-on; population
210 → 1,436 as backfill drained) but never joined. The registered rationale ("a bar
invariant to base-rate drift") does not hold in the data.

## 3. No edge survives; the apparent test-window positive is survivorship

`tools/backtest_whale_follow.py --slippage 0.01 --seed 20260905` reproduced the committed
report byte-identical on the headline lines. **No configuration is FAVORABLE.**

    Q=all   K=1 E=hold   train −0.0400 (n 22,999)  test +0.0102 (n 52,800)  live −0.0603 (n 27,026)
    Q=top20 K=1 E=hold   train −0.0385            test +0.0181             live −0.0916
    Q=top20 K=2 E=hold   train −0.0776            test +0.0347             live −0.0599

Positive cells: train 0 / test 6 / live 2 of 12. After Bonferroni-12, **zero** test cells
clear. Slippage 0.02 turns every test cell negative. Exit rule costs −0.0064/$ on average.

Independent walk-forward using the repo's own point-in-time `score_state` at four cutoffs
(2026-06-01 / 07-01 / 08-01 / 08-15), positions entered after the cutoff and resolved
within 30 d, band-adjusted realized edge per share, market-clustered bootstrap:

    scored OOS positions n=20,616 (7,716 markets)
    fires (lb80 ≥ 0)   n= 3,202   e_adj +0.0247   CI90 [+0.0037, +0.0453]
    non-fires          n=17,414   e_adj +0.0097   CI90 [+0.0002, +0.0186]
    fires − non-fires  +0.0150    CI95 [−0.0092, +0.0395]   P(diff ≤ 0) = 0.118
    Spearman ρ(lb80, OOS edge) = −0.0033

Deciles are non-monotone (D1 +0.0222 beats D2, D4–D8; best is D3 +0.0388). The sign of
fires-vs-non-fires flips across folds (−0.0440 at 06-01, +0.0826 at 07-01); one fold
carries the pooled result.

Signals that actually fired, entered at the recorded `touch_ask`, 100 of 147 resolved:

    V1 n=15 win 60.0% edge −0.0073 | V4 n=7 57.1% −0.0068 | V5 n=7 57.1% +0.0371
    V6 n=71 53.5% −0.0189 | ALL n=100 win 55.0% edge/share −0.0124 (se 0.045), gross of fees

Follower's entry cost: median `touch_ask − whale vwap` = **+0.0100** on a ~$0.55 contract
(≈1.8% of stake) before the 3% sports fee — by itself larger than the +0.0150 point
estimate. Stake size does not change any of this: every cost is proportional, and larger
size fills further from the whale's price, not closer.

Why the test window looks positive: 3,337 of 4,007 wallets come from `leaderboard_snapshots`
ordered by PNL; the MONTH/WEEK boards were snapshotted 2026-09-03..06, so their lookback
sits inside the test window. The whole universe regardless of score runs +0.0085/share OOS
(CI90 excludes 0). Overlap cohort (same 249 wallets both eras): −0.0468 train / −0.0010
test. `reports/backtest-whale-follow-2026-09-05.md` Limitation 1 names this for the
backtest; nothing in the live scoring path corrects for it, and backfill has drained only
926 of 3,337 leaderboard wallets.

Measurement defects that make every CI in the registration too narrow:
- `markets.event_key` NULL on **133,521 / 135,108 (98.8%)** → the "event-clustered"
  bootstrap is a `condition_id` bootstrap; both sides of one game resampled as independent.
- `/activity` backfill caps at 2,000 prints/wallet; 216 of 931 wallets sit exactly at the
  cap — high-frequency wallets' visible history begins recently by construction.
- Unresolved share 2.0% train / 2.0% test / **17.7% live** — cross-era comparison mixes
  censoring regimes.
- `hygiene_mat` has no `as_of`; any historical "clean" filter uses future prints.

## 4. Engineering findings (fixed 2026-09-06; see commits on rebuild-v3)

- **Kill switch computed, never enforced.** Only consumer of a `FIRED` rule was the webhook
  at `arb/commands.py:236`; the lane's sole gate was `enabled` (`arb/lanes/whale.py:109`).
  A fired rule did not stop new bookings.
- **`arb/eligibility.py` had 11 `return` branches never executed by any test**, including
  line 104 (the rule-6 non-whitelisted-pair gate) and the only two `"invalid"` returns.
- **Rule-12 grep-test vacuous**: `all([])` and `len([]) <= 1` both pass when `classify()`
  is deleted. Module docstring named seven importers; the only one is `arb/tracker.py:11`.
- **Rule 2 had no mechanism** — no network guard in `tests/conftest.py`.
- **Rule-9 grep missed** schema-qualified, `INSERT OR REPLACE`, `ON CONFLICT DO UPDATE`,
  quoted identifiers, and scanned only `arb/` (scripts/tools/ops/*.sql unscanned).
- **Invariants 1, 2, 5, 6 untested**; `bet_events` has no uniqueness on settled rows;
  `arb/metrics.py:55` takes `settled[0]` while `arb/whale_pnl.py:43` counts every settled
  row, so a duplicate settle inflates the 24 h exit count silently.
- Rule 13 (no restart on a dirty tree) has no code behind it — the supervisor never checks
  `git status`. Left as a workflow rule; noted.

No live hard-rule violation was found in `arb/`. Rule 5 is genuinely enforced by the
Decimal YAML loader (`arb/config.py:24-26`). Whale-module statement coverage traced at
86.6–94.1% across all 14 modules.

## 5. Other findings, below the fold

- `holders` gate (1b) bites in 659 / 242,561 evaluations (0.27%); `ratio == 1.0` on
  38/67 V1, 26/30 V4, 50/55 V5, 725/946 V6 fires — measuring absence of counterparties,
  not agreement. `holders_ratio IS NULL` on 56 of 67 V1 and 25 of 30 V4 fires.
- `flagged_edge` mean is negative for V1 (−0.0335) and V4 (−0.0392), positive for V5/V6;
  it is the denominator of kill rule 2, so pooling eras in `_fast_kill_window` makes the
  fast kill's sign era-dependent.
- Sybil single-linkage (`sybil_gap_secs` 5) drops 17% of V5 and 12% of V6 contributing
  whales on fired signals.
- 300 s print-age gate uses the unweighted `live` set (`arb/whale_signal.py:237`), so a
  non-voting whale's recency can pass a V4/V5 signal.
- 67,994 condition_ids with prints have `markets.sport IS NULL` (593,235 prints, 35%).
- `mm` hygiene starved by the $1,000 retention floor for tape-discovered wallets: 261 of
  4,007 flagged; coverage asymmetric vs leaderboard wallets.
- Non-sports roots at `base_rate_default` permanently: politics n=238, geopolitics 65,
  crypto 2 vs `base_rate_min_n` 500.
