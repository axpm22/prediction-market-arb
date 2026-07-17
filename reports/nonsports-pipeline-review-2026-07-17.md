# Non-sports (politics/econ/weather) pipeline — review & activation, 2026-07-17

Scope: `arb/nonsports/*` (match, scan, ai, mapped, kalshi_events), `arb/lanes/political.py`,
their config, and the live ops state. Entry point delivered: `scripts/nonsports_pipeline.sh`.

## Activation status

- **Pipeline: ACTIVE.** Supervisor alive (political / negrisk / mapped lanes enabled and
  beating, sha=f68c96a); `scripts/nonsports_pipeline.sh` ran green end to end today.
- **Haiku matcher: eval-active, enable-gated.** All 257 candidates classified and cached
  (total historical spend $1.05). Current §18.5 score vs human ground truth:
  **precision 1.0 — TP 9 / FP 0 / FN 0 / TN 248** at min_confidence 0.85.
  `ai_matcher.enabled` stays **false**: the pre-registered bar requires ≥ 80 verdicted
  pairs incl. ≥ 50 same on pairs curated AFTER the 2026-07-13 design freeze; only 9
  positives exist, all pre-freeze/dev. Nothing to fix — positives accumulate as future
  months' econ markets list. Flipping the flag is a dated PREREGISTRATION amendment only.

## Was it ineffective, and is that fixable?

Two separate "ineffective" findings:

1. **Haiku v1 was ineffective** (first eval 2026-07-13: TP 0 / FN 9 — missed every true
   pair). **Already fixed** by the v3 architecture split: proposition *shape* (thresholds,
   inversion, boundaries) decided in code (`match.shape_relation`); Haiku answers only
   same-source/series/entity/period. Dev precision went 0 → 1.0 with zero false positives.
   Remaining blocker is evidence volume, not capability — fixable only by time + curation.

2. **The whole PM half of the program went dead on 2026-07-16 — NOT fixable in code.**
   `gamma-api.polymarket.com` and `clob.polymarket.com` resolve (via the ISP resolver
   192.168.1.1) to 145.239.225.117 serving a `CN=*.anj.fr` certificate — the Autorité
   Nationale des Jeux block server. Every PM fetch dies with CERTIFICATE_VERIFY_FAILED
   (225 failures 07-16, 277 today). Consequences while it lasts:
   - `political` lane (PM-only by design): zero quotes.
   - `mapped` lane: 9/9 pair errors per poll (PM leg), Kalshi legs still persist.
   - matcher (`arb nonsports`): cannot refresh candidates → no new post-freeze pairs
     → the §18.5 positive count cannot grow → the Haiku gate is frozen in place too.
   - Kalshi is healthy (200 in ~0.7s; this morning's Kalshi DNS errors were transient).

   This is a regulatory/network condition. Options are the operator's call, not the
   codebase's: (a) accept Kalshi-only operation (kalshi_events baskets still run);
   (b) network-level changes (different resolver / VPN) — note that would be routing
   around a French regulator's block, a legal judgment I'm not making for you;
   (c) pause the PM lanes and record the outage window in the pre-registration log so
   the 3-week kill gates aren't judged on a blocked venue.

## Faults found (code)

- **F1 — retry storm on a permanent failure.** `base.py` retries + backs off on the ANJ
  cert failure every poll, ~500 warnings/day and wasted per-cycle latency. An
  `SSLCertVerificationError` is non-retryable; catch it, log the venue as BLOCKED once
  with the served CN, and short-circuit the venue for the rest of the poll (simple
  circuit breaker). Smallest real fix on the list.
- **F2 — WAL checkpoint starvation (ops).** `data/arb.sqlite` = 13.8 GB but its WAL is
  **15.2 GB** — checkpoints never complete, almost certainly a long-lived reader
  (dashboard and/or watch loop holding cursors). Disk cost ~2×, crash-recovery slow,
  reads degrade. Fix: `PRAGMA wal_checkpoint(TRUNCATE)` in `cmd_backup` (04:10, quiet
  window) and audit for connections held open across polls. No schema change.
- **F3 — `evaluate()` ground-truth filter.** Accepts only human verdicts `same`/`different`;
  a curator writing `same-inverted` (the AI's own vocabulary, and the draft_note style)
  would silently drop the pair from scoring. One-line hardening:
  `v.get("verdict","").startswith("same") or == "different"`.
- **F4 — AI budget counts free verdicts.** Shape-only pairs (code-decided, no API call)
  still consume `max_new_pairs_per_day` (both `classified` and `_spent_today` count
  them). With 248/257 pairs shape-rejected, a sweep burns 257 budget for ~9 real calls.
  Harmless at budget=900, misleading if the budget is ever tightened.
- **F5 — shape-input asymmetry.** `run_ai` feeds Kalshi text+rules but only PM *text*
  to `shape_relation`; a PM threshold stated only in the description parses as
  `unknown` → conservative-different. Recall loss only (safe direction), worth a note
  in the file.
- **F6 — supervisor process age.** The supervisor python process predates the last five
  commits (children re-import current code at each spawn, so behavior is current; the
  parent's scheduling code is not). Restart it at the next quiet window.
- **F7 — same-volume backups.** Startup warning is real: `backups/` is on the DB's disk.
  One disk failure loses ledger + backups (§13.3).

## Improvements / speed

- **S1 — batch the mapped lane (biggest speed win).** Today: 2 sequential requests per
  pair per poll (Gamma GET + one `POST /books` with that pair's 2 tokens) = 18 requests
  for 9 pairs. `POST /books` is already the verified batch endpoint — send ALL pairs'
  tokens in ONE call, and batch the Gamma status check if `condition_ids` accepts a
  comma list (verify live per rule 3 before relying on it). ~18 → 2-3 requests/poll.
- **S2 — parallelize the matcher's Kalshi side.** `_kalshi_side` fetches up to 150
  series sequentially inside a 240s deadline and logs truncation. A 8-thread pool (it's
  pure I/O) cuts it ~8×; or fetch via the global `/markets?status=open` listing (verified
  2026-07-14) and bucket by series locally — one paginated stream instead of 150 GETs.
- **S3 — `_cached()` per-candidate query.** One `SELECT` per pair per run; a single
  `SELECT pair_key FROM verdicts WHERE rules_sha=? AND model=?` into a set is one query.
  Milliseconds today, but it's also 257 round trips into a 13.8 GB live DB.
- **S4 — political lane parses each book twice.** `book_quote_rows` and `scan_market`
  independently recompute best-ask/bid/size over the same books dict. Merge into one
  pass returning (rows, opportunity). Maintenance more than speed.
- **S5 — candidates YAML round-trip.** 683 KB YAML fully parsed+rewritten each AI run
  even when only annotations change; fine at 257 pairs, will crawl at thousands.
  Leave until it hurts.

None of these were applied today — the tree got only the additive script + this report;
lane code is live under the supervisor and behavior changes belong in their own
committed, tested change (rule 13). Suggested order: F1, F2, S1 (each is small,
testable, and pays back immediately), then F3 opportunistically.
