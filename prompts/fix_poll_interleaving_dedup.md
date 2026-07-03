# Task: fix bet dedup broken by multi-process poll interleaving (2026-07-02)

Read `CLAUDE.md` and `SPEC.md` first. All hard rules apply — `flat_bets` is append-only
(never update/delete rows), fixtures-only tests, no magic numbers.

## The bug (verified against the live DB)

The tracker's lifetime rule — "fingerprint absent for ≥1 completed poll → bet closed;
reappears → new bet" — was written when `watch` was the only process writing `polls` rows.
Now `political`, `ev-exit`, `focus`, `negrisk`, and `hot` also insert poll rows, interleaved
between watch polls. Those polls contain no sports quotes, so every watch-poll fingerprint
looks "absent" on the very next completed poll, gets closed, and "reappears" one watch cycle
later as a brand-new bet.

Observed effects (all in today's data):

1. **Duplicate bets:** fingerprint `c2f39d1b…` (Braves–Cardinals EV) booked as a new $1 bet
   every ~165 s — 29 rows across 2 fingerprints, each chained by `reappearance_of`. When the
   game won, the win was counted ~10×. This is why `pnl_optimistic` looks great (9.42 vs
   2.23 confirmed on MLB EV).
2. **Confirmed-fill starvation:** `pnl_confirmed` requires the fingerprint on the second
   *consecutive* poll — which a non-watch poll always breaks — so 50 of 52 rows have
   confirmed = NULL. The two columns are broken in opposite, mutually flattering directions.

## The fix

**Lifetime, dedup, and confirmed-fill must only ever compare a fingerprint against the
previous completed poll OF THE SAME KIND as the one that created it.**

1. In `tracker.py`, wherever "was this fingerprint present on the previous poll" is asked
   (bet creation dedup, lifetime close, confirmed-fill second observation), the previous
   poll is `SELECT … FROM polls WHERE kind = ? AND id < ? ORDER BY id DESC LIMIT 1` — the
   creating poll's own kind, never just `id - 1` / max(id). Audit every query in
   `tracker.py`, `lifecycle.py`, and `paper.py` that joins or compares against `polls`;
   fix each one the same way. Grep for `poll_id` comparisons across the whole `arb/`
   package — any consumer assuming globally-consecutive poll ids for lifetime logic has
   this bug.
2. Bets created by non-watch processes (political, negrisk, btc…) get the same rule within
   their own poll kind — the fix is symmetric, not watch-special-cased.
3. Add `kind` to whatever index the lookup uses so this stays O(1) per fingerprint.

## Repair the damaged data — without editing rows

`flat_bets` is append-only; the ~27 duplicate rows stay. Reuse the `bet_annotations`
mechanism (from `prompts/stop_inplay_betting_fix.md`; build it here if that prompt hasn't
run yet — same schema: `bet_annotations(id, bet_id, annotated_at, reason, notes)`):

1. Detection query: within each fingerprint, order rows by `opened_poll_id`; any row whose
   predecessor (same fingerprint) closed on the immediately-preceding poll *of a different
   kind* is an interleaving artifact — annotate all but the FIRST row of each such chain
   with reason `dedup-interleave-duplicate`, storing the kept row's id in notes.
2. Print the full candidate set and require `--yes` before writing (same convention as the
   in-play annotation command).
3. Report: annotated duplicates excluded from every P&L section, counted in the
   "voided — data error" line. After annotation, recompute today's daily report and show
   MLB EV optimistic P&L with duplicates removed — expect roughly one game's worth of P&L
   per fingerprint, not ten.

## Tests (fixtures only)

1. Regression: simulated sequence watch-poll → political-poll → watch-poll where the same
   fingerprint appears on both watch polls. Expect: ONE bet, still active, and
   `pnl_confirmed` filled from the second watch poll's price. (Under the old logic this
   produced two bets and confirmed=NULL — assert that specifically doesn't happen.)
2. Lifetime close still works within a kind: watch → watch (fingerprint absent) → watch
   (present again) = two bets linked by `reappearance_of`.
3. Symmetry: same scenario with `political` polls interleaved by `ev-exit` polls.
4. The §9 tracker test vector from SPEC.md must still pass unchanged (it's single-kind, so
   it should — if it doesn't, the fix is wrong, stop and report).
5. Annotation detection test: fixture DB reproducing today's chain shape → exactly the
   duplicate rows (not the first of each chain) are selected.

## Acceptance

1. Full `pytest` green, including the §9 vector untouched.
2. The annotation run's printed row set for today's real DB (do not write without showing it).
3. `report --daily 2026-07-02` before/after: optimistic MLB EV drops from ~9.42 to a
   deduplicated figure; confirmed-fill NULL rate for new bets (post-fix) drops in the next
   live session — state this as the expected observable, it can't be proven in this run.
4. One live `watch --once` after the fix showing no `reappearance_of` chains created for
   fingerprints that persisted across consecutive watch polls.
