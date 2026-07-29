# Phase 3 — Fetch efficiency and coverage (CORRECTED)

**Supersedes Phase 3 of `2026-07-29-speed-and-arb-survival.md`.** Phases 1 and 2 of
that plan are complete (commits `09059bd`, `f0ccfa5`, `0f5dfea`, `5ce3791`,
`3214f5d`, `487cd22`, `28e28bb`, `aa68107`). Phase 4 is unchanged and still
applies — only its line numbers have drifted, recorded in §4 below.

This document exists because **two of the original Phase 3 tasks are wrong**, not
merely stale. Task 9 would have gated off the sports that work while keeping the
one it was written to remove; Task 10's Polymarket half is a no-op against
parallel code. Both were caught by reproducing the plan's own claims before
implementing, per the CLAUDE.md workflow.

## What changed underneath the original plan

The original was written while a concurrent session (this one) was editing
`arb/venues/*.py`, `arb/matching.py`, `config.yaml` and `data/aliases.json`. That
work has since landed:

| Commit | Effect relevant to Phase 3 |
|---|---|
| `9a1a7fe` | Configured sports 2 → 9; `KXNBA` (futures) → `KXNBAGAME` (games) |
| `923f94a` | Polymarket 3-way soccer decomposed; MLS aliases; soccer pairs live |
| `bbf8485` | **SX sportId → league mapping**; `baseball_npb`/`baseball_kbo` added |
| `166292d` | 2026-07-29 venue survey recorded in `data/verified_facts.md` |

Sports list is now 11: `baseball_mlb, basketball_nba, basketball_wnba,
americanfootball_nfl, icehockey_nhl, soccer, tennis, mma, cricket, baseball_npb,
baseball_kbo`. Cross-venue matched groups went 40 → 118.

## Global constraints

Unchanged from the original plan (§Global Constraints). Rules 1–5, 7, 9, 12, 13,
the SPEC §16 workflow, the ~300-line module cap, and `pytest -q` green before
every commit. Note **SPEC §10 was amended 2026-07-29** (lifetime closure now
needs `tracker.dead_checks` consecutive misses) — do not "restore" the old
one-poll rule if a test looks odd.

## Pre-flight

- [ ] **Step 0.1: Tree state.** `git status --short` — the venue adapters are
      committed now, so Phase 3 is unblocked. `arb/invariants.py` is being
      edited in a separate session (invariant-3 live-lane exemption); **do not
      touch that file**.
- [ ] **Step 0.2: Green baseline.** `.venv/bin/python -m pytest -q` → 356 passed.
      NB: bare `python`/`pytest` resolve to anaconda 3.12 and lack deps.
- [ ] **Step 0.3: Report baseline.** `.venv/bin/python -m arb report --daily 2026-07-28 > /tmp/report-baseline.txt`.
      Diff it after every task, ignoring the `head=` and `quote age=` lines —
      both move on their own while the supervisor runs.

---

# §1 Task 9 — REWRITTEN: activity gating with a per-sport lookahead

## Why the original is wrong

The original gates a sport in if it has a fixture inside a flat
`activity_lookahead_hours: 48`. Measured against the production DB 2026-07-29,
over quotes fetched in the previous 20 minutes:

```sql
WITH recent AS (SELECT sport, start_utc FROM quotes
                WHERE julianday(fetched_at) > julianday('now') - 20.0/1440
                  AND start_utc IS NOT NULL)
SELECT sport,
       ROUND(MIN(CASE WHEN (julianday(start_utc)-julianday('now'))*24 > 0
                      THEN (julianday(start_utc)-julianday('now'))*24 END),1) next_h,
       SUM(CASE WHEN (julianday(start_utc)-julianday('now'))*24 BETWEEN 0 AND 48
                THEN 1 ELSE 0 END) in_48h
FROM recent GROUP BY sport ORDER BY next_h;
```

**Filter the timestamp with `julianday()`, never `fetched_at > datetime('now',…)`.**
`fetched_at` is ISO-8601 with a `T` separator and a `+00:00` suffix, while
`datetime('now')` renders with a space; the string comparison is true for every
row from the same date, so the naive form silently does not filter at all. An
earlier draft of this table was wrong for exactly that reason.

| sport | next fixture | fixtures within 48h | cross-venue pairs today |
|---|---|---|---|
| tennis | 0.2 h | 4,576 | 28 |
| baseball_mlb | 1.2 h | 9,146 | 41 |
| soccer | 1.4 h | 24,840 | 15 |
| cricket | 4.4 h | 2,760 | 0 |
| basketball_wnba | 5.4 h | 972 | 8 |
| baseball_kbo | 14.9 h | 1,200 | 0 |
| baseball_npb | 38.4 h | 720 | 0 |
| **mma** | **67.4 h** | **0** | **9** |
| **americanfootball_nfl** | **197.4 h** | **0** | **17** |

`basketball_nba` and `icehockey_nhl` do not appear at all — they receive zero
quotes from every venue, which is the point made in the next section.

`mma` and `americanfootball_nfl` have **zero** fixtures inside 48 h and would be
gated off. Between them they carry **26 of the 118 cross-venue pairs**. The
re-probe clause does not save them: it would drop those two sports from a 60 s
cadence to a 3600 s one, and an arb lifetime is minutes.

The cause is that 48 h encodes an assumption about fixture cadence that is only
true for daily sports. UFC cards are weekly; NFL games are weekly and list ~8
days ahead. **The window width was never the discriminator.**

Separately, the original's stated justification for deleting `scan_breaks` /
`break_windows` — "SPEC §13.1 promises a halftime re-scan for NBA and soccer,
and both sports have produced zero flags in 21 days" — no longer holds. Soccer
produces 15 cross-venue pairs as of `923f94a`. **Delete the keys anyway**, but on
the honest ground: no code has ever read them (`grep -rn scan_breaks arb/`
returns only the schema line), so they assert capability that does not exist.
That argument is independent of any sport's yield.

## What actually discriminates

Out-of-season sports have **no dated game markets at all**, at any horizon — not
"none within 48 h". Verified after `bbf8485`:

```
sqlite3 data/arb.sqlite "SELECT venue, sport, COUNT(*), MAX(fetched_at)
  FROM quotes WHERE sport IN ('basketball_nba','basketball_wnba')
  AND fetched_at > datetime('now','-12 minutes') GROUP BY venue, sport"

polymarket|basketball_wnba|7956|18:26:32
kalshi    |basketball_wnba|9816|18:26:32
sx        |basketball_wnba| 344|18:26:32
sx        |basketball_nba |15653|18:02:29   <- stops at the bbf8485 deploy
kalshi    |basketball_nba |61080|17:07:20   <- stops at the 9a1a7fe deploy
```

`basketball_nba` now receives **zero** quotes from every venue. The two fixes
this session already removed the 439,152-quote problem the task was written for
— the NBA quotes were SX's WNBA games and Kalshi's championship-futures series
wearing the NBA name. Activity gating is now a **cost** optimisation, not a
correctness one, and it must be built so it cannot cost coverage.

## Design

Per-sport lookahead, defaulting to the global value, with overrides derived from
the measured table above and a wide margin. Every value in `config.yaml` (rule 4).

- [ ] **Step 1: Config.** Replace `activity_lookahead_hours` and delete the dead
      break keys.

```yaml
activity_lookahead_hours: 48        # DEFAULT: sport ACTIVE iff a venue lists a
                                    #   fixture within this window...
activity_lookahead_overrides:       # ...but fixture cadence is not uniform.
  mma: 240                          #   UFC cards are weekly; measured next
                                    #   fixture 67.6h on 2026-07-29 -> a flat 48h
                                    #   gated off a sport with 9 cross-venue pairs
  americanfootball_nfl: 336         #   weekly games listed ~8 days out; measured
                                    #   197.6h -> would have gated off 17 pairs
  baseball_npb: 96                  #   measured 38.6h, thin schedule
  cricket: 240                      #   Test/ODI series list far ahead
                                    # NB overrides only ever WIDEN. A sport whose
                                    #   season has ended has no dated fixture at
                                    #   ANY horizon (verified: basketball_nba
                                    #   receives zero quotes from all three
                                    #   venues after bbf8485), so widening cannot
                                    #   readmit an out-of-season sport.
activity_reprobe_secs: 3600         # ...OR its newest stored quote is older than
                                    #   this. Without the re-probe the gate is a
                                    #   trapdoor: a gated sport stops producing
                                    #   quotes, so it could never prove itself
                                    #   active again.
```

Delete `scan_breaks:` and `break_windows:` (config.yaml, currently ~line 60) and
their continuation comments.

- [ ] **Step 2: Schema.** In `arb/config.py`: add
      `"activity_lookahead_overrides": MAP(NUM), "activity_reprobe_secs": NUM`;
      remove `"scan_breaks"` and `"break_windows"`.

- [ ] **Step 3: Index.** `arb/schema.py` `MIGRATION_7` and `arb/migrations.py` —
      **unchanged from the original plan**, both indexes still needed
      (`quotes(sport, start_utc)`, `quotes(sport, fetched_at)`); the quotes table
      is now larger, not smaller.

- [ ] **Step 4: Tests.** Take the original's `tests/test_activity.py` and **add
      these three**, which are the regressions this rewrite exists to prevent:

```python
def test_weekly_cadence_sports_are_not_gated_off(cfg, tmp_path):
    """A flat 48h lookahead gated off mma (next fixture 67.6h) and
    americanfootball_nfl (197.6h) on 2026-07-29 — 26 of the 118 cross-venue
    pairs. Fixture cadence is not uniform across sports."""
    store = Store(tmp_path / "t.sqlite")
    _quote(store, "mma", NOW + timedelta(hours=67.6), NOW)
    _quote(store, "americanfootball_nfl", NOW + timedelta(hours=197.6), NOW)
    active = active_sports(cfg, store, NOW)
    assert "mma" in active
    assert "americanfootball_nfl" in active


def test_overrides_cannot_readmit_an_out_of_season_sport(cfg, tmp_path):
    """Overrides only widen a window. An out-of-season sport has no dated
    fixture at ANY horizon, so no width readmits it."""
    store = Store(tmp_path / "t.sqlite")
    _quote(store, "basketball_nba", None, NOW)          # listed, but undated
    _quote(store, "baseball_mlb", NOW + timedelta(hours=6), NOW)
    assert "basketball_nba" not in active_sports(cfg, store, NOW)


def test_every_override_is_a_configured_sport(cfg):
    """A typo'd override key is silently ineffective — the worst kind of knob."""
    for sport in cfg["activity_lookahead_overrides"]:
        assert sport in cfg["sports"], sport
```

- [ ] **Step 5: Implement.** As the original's `arb/activity.py`, with the
      lookahead resolved per sport:

```python
    look = float(cfg["activity_lookahead_overrides"].get(
        sport, cfg["activity_lookahead_hours"]))
```

  computed **inside** the per-sport loop, not once above it.

- [ ] **Steps 6–9: Wiring.** `arb/watch.py` `fetch_all_venues(cfg, store=None)`
      and the `cmd_watch` call site — **unchanged from the original plan**.

- [ ] **Step 10: Verify against the real DB, no network.** Run the original's
      snippet. **Expected now:** the list drops `basketball_nba` and
      `icehockey_nhl` and keeps all nine others — in particular `mma` and
      `americanfootball_nfl`, which is the whole point of this rewrite. If either
      is missing, the override map did not load.

- [ ] **Step 11–12: Suite and commit.** Record the before/after active list in
      the commit message.

---

# §2 Task 10 — CORRECTED: rotate SX only

## Why the Polymarket half is a no-op

The original applies `rotate` to `arb/venues/polymarket.py` `fetch_markets` on
the grounds that "the shared `venue_deadline_secs` truncates the same tail every
time". That is true of a **serial** loop. Polymarket's is not serial:

```python
with ThreadPoolExecutor(max_workers=max(1, len(sports))) as pool:
    per_sport = list(pool.map(lambda s: _fetch_sport(cfg, s, deadline), sports))
```

`max_workers == len(sports)`, so every sport starts concurrently and races the
same wall clock. Reordering the input list changes nothing about which sport the
deadline truncates. The same is now true of `arb/venues/sx.py` `fetch_markets`,
which fans out over distinct sportIds.

- [ ] **Do:** implement `rotate` in `arb/venues/base.py` and its two tests
      exactly as the original Step 1–4.
- [ ] **Do:** apply it to the **SX league loop**, which is genuinely serial
      inside one sportId. Line has drifted: it is now
      `arb/venues/sx.py:~146` (`for lg in leagues:`), after the
      `league_sport`/`sport_ids_to_fetch` additions in `bbf8485`.
- [ ] **Do NOT:** apply it to `polymarket.fetch_markets` (original Step 6). Skip
      that step entirely and say so in the commit message, or a future reader
      will "restore" it.
- [ ] **Consider instead:** the real PM truncation risk is the **10-page Gamma
      cap on the soccer tag**, measured 2026-07-29 — 2,100+ soccer events exist,
      all 19 MLS-matchable ones fall inside the cap, but **14 of them sit on page
      10 exactly**. That is one event away from silent coverage loss. The adapter
      logs `gamma pagination cap hit`, so it is not silent, but this deserves its
      own task with a measurement rather than a rotation.

---

# §3 Tasks 11–13 — line numbers corrected, premises re-verified

## Task 11 — skip CLOB books for untraded markets

**Premise re-verified 2026-07-29:** 2,744,916 of 4,648,780 Polymarket quotes in a
12-minute window carry `volume_24h = 0` — **59.0%** (the original said 57.8%,
measured before the sports expansion). The task stands.

- **Line drift:** the `wanted = []` construction is now
  `arb/venues/polymarket.py:158`, and `fetch_books(cfg, wanted, ...)` is at
  `:163` — the original cited 122–128, before `_proposition_outcome` and the
  soccer decomposition were added in `923f94a`.
- **Extra care:** `_books_wanted` must be inserted **above** `_fetch_sport` and
  must keep the `sportsMarketType in IN_SCOPE_TYPES` check. Soccer markets are
  now three propositions per event rather than one 3-outcome market, so the token
  count per event changed; the filter logic does not.
- Everything else — the three tests, the missing-field-is-unknown rule, the
  `books_min_volume_24h: 0` escape hatch — is unchanged and correct.

## Task 12 — SX orders chunk to config

- **Line drift:** `for i in range(0, len(markets), 20)` is now
  `arb/venues/sx.py:149` (was 112).
- The `test_sx_orders_chunk_comes_from_config` regex test is unaffected — it
  greps the source rather than citing a line.
- **Verify-live step still mandatory.** SX is the flakiest venue in the system;
  if chunk 30 returns 4xx, revert to 20 in config per rule 3.

## Task 13 — cache the MLB schedule per NY date

- `fetch_mlb_schedule` is still at `arb/cycle.py:53`. **No drift, no correction.**
- Implement exactly as written, including the deliberate choice not to cache
  failures.

---

# §4 Phase 4 — line numbers only

Tasks 14 and 15 are correct as written. Drift:

| Original | Now |
|---|---|
| `config.yaml:200-201` (`hot:` block) | `config.yaml:236` |
| `arb/config.py:112-113` (`"hot"` schema) | `arb/config.py:113` |
| `arb/cli.py:66` (`hot` subcommand) | unchanged, `arb/cli.py:66` |

**Task 14 note:** the `hot:` block still contains its own `dead_checks: 3`. Task 1
added a separate `tracker.dead_checks`, so both currently exist; deleting the
`hot:` block removes the duplicate. `SPEC.md:749` still shows `dead_checks` inside
the `hot` block — update that line in the same commit.

**Task 15 note:** its Step 3 death-attribution text should now read
"fixed 2026-07-29 by `tracker.dead_checks`" as a completed item, and its Step 4
uptime text should cross-reference `heartbeat.stale_lanes`, both of which landed
in Phase 1/2.

---

# §5 Deliberately still not in this plan

Carried over from the original, plus two new:

- **Any cadence number change.** The mechanism shipped in Task 7
  (`supervisor.cadence_windows`, empty). The numbers remain a pre-registered
  policy decision.
- **Dropping soccer from `sports:`.** No longer even a candidate — soccer
  produces 15 cross-venue pairs since `923f94a`.
- **Soccer aliases beyond MLS.** Kalshi lists 192 distinct clubs, Polymarket 483,
  and exactly 2 match as written. Each competition needs its own hand-verified
  alias block; UCL / Liga MX / Brasileirão are the same recipe, one at a time.
- **`invariant-3`'s live-lane exemption.** Being fixed in a separate session; do
  not touch `arb/invariants.py`.
- **The Gamma soccer pagination cap.** See §2 — needs its own measured task.
