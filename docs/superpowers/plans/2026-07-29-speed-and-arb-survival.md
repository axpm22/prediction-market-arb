# Speed & Arb-Survival Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop losing flagged arbitrages to bookkeeping deaths, make the supervisor's cadence honest and observable, and cut the wasted 76% of every poll — so that a later cadence decision is made on clean measurements.

**Architecture:** Four independently shippable phases. Phase 1 changes ledger lifetime semantics in `arb/tracker.py` only (zero network, zero new requests). Phase 2 changes the supervisor's scheduler in `arb/heartbeat.py` (zero network). Phase 3 changes venue adapters and adds one new module `arb/activity.py` (reduces requests). Phase 4 is docs and ops hygiene. **Halt between phases is safe and expected** — each ends with a green `pytest` and an unchanged `arb report --daily`.

**Tech Stack:** Python 3.11+, SQLite via `arb/store.py`, `pytest`, `rich`, `requests`. No new dependencies.

## Global Constraints

Copied verbatim from `CLAUDE.md` / `SPEC.md` §4. Every task's requirements implicitly include this section.

- **Rule 1 — Paper trading only.** Never call authenticated or order-placement endpoints. Public unauthenticated GETs/POSTs only.
- **Rule 2 — Never spend Odds API credits in tests or loops.** All tests run against recorded fixtures in `tests/fixtures/`. No task in this plan may add an Odds API call.
- **Rule 3 — Do not invent API endpoints or fields.** Only endpoints in `SPEC.md` §6 or dated in `data/verified_facts.md`.
- **Rule 4 — No magic numbers.** Every threshold, rate, and schedule time lives in `config.yaml` and is declared in `arb/config.py:SCHEMA`.
- **Rule 5 — Money math uses `Decimal`, never float.**
- **Rule 7 — Persist every quote observed during a scan**, even when no opportunity is found. *Not fetching* a market is permitted; *discarding* a fetched quote is not.
- **Rule 9 — `flat_bets` is append-only.** Never `UPDATE`/`DELETE` `flat_bets` or `bet_events`. New facts are new `bet_events` rows. A CI grep-test in `tests/test_hygiene.py` enforces this.
- **Rule 12 — One eligibility predicate.** Only `arb/eligibility.py` may define `classify(`.
- **Rule 13 — Nothing restarts on a dirty tree.** Loops run from a committed HEAD after a green `pytest`.
- **Workflow (SPEC §16):** Reproduce first → failing test → minimal implementation → full `pytest -q` → commit with evidence.
- **Modules stay under ~300 lines.**
- **Full test command:** `pytest -q` from the repo root. It must be green before every commit.

## Pre-flight

**The working tree is dirty and a concurrent session is editing `arb/venues/kalshi.py`, `arb/venues/polymarket.py`, `arb/matching.py`, `config.yaml`, and `data/aliases.json`** (adding aliases and fixtures for soccer/tennis/mma/wnba/nfl). Phase 3 touches those same files.

- [ ] **Step 0.1: Confirm the tree state before starting**

Run: `git status --short`
Expected: if `arb/venues/polymarket.py` or `arb/venues/kalshi.py` are still listed as modified (` M`), **stop and coordinate** before starting Phase 3. Phases 1, 2, and 4 do not touch those files and may proceed regardless.

- [ ] **Step 0.2: Establish the green baseline**

Run: `pytest -q`
Expected: all tests pass. If they do not, the baseline is broken — fix or stash before starting.

- [ ] **Step 0.3: Record the report baseline**

Run: `arb report --daily 2026-07-28 > /tmp/report-baseline.txt; wc -l /tmp/report-baseline.txt`
Expected: a non-empty file. Every phase re-runs this and diffs against it; the diff must be empty except where a task says otherwise.

---

# Phase 1 — Stop losing arbs (ledger semantics)

Zero new network requests. This phase is worth more than any cadence change: measured, **19.5% of watch-lane lifetime closures were flickers** where the position was still there 60–120s later, and **68% of deaths are markets vanishing from the venue payload**, which no polling rate recovers.

### Task 1: Tolerate transient absence before closing a lifetime

Today `arb/tracker.py:190` closes a bet's lifetime the first poll its fingerprint is absent, with `reason: "absent-one-poll"`. All 128 `lifetime_closed` rows in the production DB carry that reason. Measured against the DB: of watch bets closed this way, **14.6% reappeared at the very next poll** and 4.9% two polls later. Each reappearance mints a brand-new `flat_bets` row, which is what drives the reappearance storm in Task 2.

Separately, the venue-gap carry rule at `arb/tracker.py:182-188` has no bound: bet 126 has been carried open **300 times** because sx's median `n_quotes` in live polls is 0. Both are the same code path, so both are fixed here.

`bet_events` is append-only (rule 9), so "consecutive misses" cannot be a counter column. It is derived: count `sighting_missed` rows newer than the most recent `sighting_seen` row.

**Files:**
- Modify: `arb/tracker.py:181-194` (the `else:` branch of the sighting loop) and `arb/tracker.py:151-180` (the sighted branch, to write the recovery marker)
- Modify: `config.yaml` (add `tracker.dead_checks`)
- Modify: `arb/config.py:71` (schema entry)
- Test: `tests/test_tracker.py`

**Interfaces:**
- Produces: `arb.tracker._consecutive_misses(store, bet_id: int) -> int`
- Produces: two new `bet_events.kind` values, `"sighting_missed"` and `"sighting_seen"`. `bet_events.kind` is free-form `TEXT` (`arb/schema.py:72`) — no migration needed.
- Produces: two new `lifetime_closed` reasons, `"absent-n-polls"` and `"venue-gap-timeout"`, replacing `"absent-one-poll"`.
- Consumes: `cfg["tracker"]["dead_checks"]`.

- [ ] **Step 1: Add the config key**

In `config.yaml`, replace the `tracker:` block (currently at line 99):

```yaml
tracker:
  terminal_mute_grace_mins: 180
  max_reappearances: 10             # NOT observability — this SUPPRESSES booking (see
                                    #   reappearance_cap(); scaled by cadence)
  dead_checks: 3                    # consecutive missed sightings before a lifetime closes.
                                    #   Measured 2026-07-28: 19.5% of watch closures under the
                                    #   old absent-one-poll rule came back within 1-2 polls —
                                    #   those were bookkeeping deaths, not edges closing.
                                    #   Also bounds the venue-fetch-gap carry (bet 126 was
                                    #   carried 300 polls by a chronically-down sx).
```

- [ ] **Step 2: Declare it in the schema validator**

In `arb/config.py`, change line 71 from:

```python
    "tracker": {"terminal_mute_grace_mins": NUM, "max_reappearances": NUM},
```

to:

```python
    "tracker": {"terminal_mute_grace_mins": NUM, "max_reappearances": NUM,
                "dead_checks": NUM},
```

- [ ] **Step 3: Write the failing tests**

Append to `tests/test_tracker.py`. These reuse the module's existing `cfg` fixture, `arb_opp()` helper, `NOW`, and `VENUE_OK` constants (defined at the top of that file).

```python
def _poll(store, kind, when):
    return store.insert("polls", {"started_at": when.isoformat(), "kind": kind,
                                  "n_quotes": 2, "n_opps": 1})


def _events(store, bet_id, kind):
    return store.query("SELECT * FROM bet_events WHERE bet_id=? AND kind=? ORDER BY id",
                       (bet_id, kind))


def test_one_missed_sighting_does_not_close_the_lifetime(cfg, tmp_path):
    """19.5% of production closures were flickers: the position was still there
    60-120s later. One absence is noise, not death."""
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    process_poll(cfg, store, _poll(store, "watch", NOW), "watch", [opp], VENUE_OK, now=NOW)
    bet_id = store.query("SELECT id FROM flat_bets")[0]["id"]

    t1 = NOW + timedelta(seconds=60)
    process_poll(cfg, store, _poll(store, "watch", t1), "watch", [], VENUE_OK, now=t1)

    assert _events(store, bet_id, "lifetime_closed") == []
    assert len(_events(store, bet_id, "sighting_missed")) == 1


def test_dead_checks_consecutive_misses_close_the_lifetime(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    process_poll(cfg, store, _poll(store, "watch", NOW), "watch", [opp], VENUE_OK, now=NOW)
    bet_id = store.query("SELECT id FROM flat_bets")[0]["id"]

    for i in range(1, int(cfg["tracker"]["dead_checks"]) + 1):
        t = NOW + timedelta(seconds=60 * i)
        process_poll(cfg, store, _poll(store, "watch", t), "watch", [], VENUE_OK, now=t)

    closed = _events(store, bet_id, "lifetime_closed")
    assert len(closed) == 1
    assert json.loads(closed[0]["payload_json"])["reason"] == "absent-n-polls"
    assert json.loads(closed[0]["payload_json"])["misses"] == 3


def test_reappearance_resets_the_miss_counter(cfg, tmp_path):
    """bet_events is append-only (rule 9), so 'consecutive' must be DERIVED from
    a recovery marker, not stored as a counter."""
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    process_poll(cfg, store, _poll(store, "watch", NOW), "watch", [opp], VENUE_OK, now=NOW)
    bet_id = store.query("SELECT id FROM flat_bets")[0]["id"]

    # miss, miss, SEEN, miss, miss -> still open (never 3 in a row)
    for i, sightings in enumerate(([], [], [opp], [], []), start=1):
        t = NOW + timedelta(seconds=60 * i)
        process_poll(cfg, store, _poll(store, "watch", t), "watch", sightings,
                     VENUE_OK, now=t)

    assert _events(store, bet_id, "lifetime_closed") == []
    assert len(_events(store, bet_id, "sighting_seen")) == 1
    # and no second flat_bets row was minted for the reappearance
    assert len(store.query("SELECT id FROM flat_bets")) == 1


def test_venue_gap_carry_is_bounded(cfg, tmp_path):
    """bet 126 was carried open 300 polls by a chronically-down sx. The carry is
    a grace period, not an indefinite hold."""
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    process_poll(cfg, store, _poll(store, "watch", NOW), "watch", [opp], VENUE_OK, now=NOW)
    bet_id = store.query("SELECT id FROM flat_bets")[0]["id"]

    down = {**VENUE_OK, "kalshi": {"ok": False, "n": 0, "latency_ms": 1.0}}
    for i in range(1, int(cfg["tracker"]["dead_checks"]) + 1):
        t = NOW + timedelta(seconds=60 * i)
        process_poll(cfg, store, _poll(store, "watch", t), "watch", [], down, now=t)

    closed = _events(store, bet_id, "lifetime_closed")
    assert len(closed) == 1
    assert json.loads(closed[0]["payload_json"])["reason"] == "venue-gap-timeout"
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `pytest tests/test_tracker.py -k "missed or dead_checks or reappearance_resets or venue_gap_carry" -v`
Expected: 4 FAILED. `test_one_missed_sighting_does_not_close_the_lifetime` fails on `assert _events(...) == []` because the current code closes immediately; the others fail on missing `sighting_missed`/`sighting_seen` rows.

- [ ] **Step 5: Add the derived-counter helper**

In `arb/tracker.py`, immediately after `_active_bets` (which ends at line 83), insert:

```python
def _consecutive_misses(store, bet_id: int) -> int:
    """Misses since the last recovery. bet_events is append-only (rule 9), so
    'consecutive' is DERIVED: count sighting_missed rows newer than the newest
    sighting_seen. No column is ever updated."""
    return store.query(
        """SELECT COUNT(*) c FROM bet_events
           WHERE bet_id=? AND kind='sighting_missed'
             AND id > COALESCE((SELECT MAX(id) FROM bet_events
                                WHERE bet_id=? AND kind='sighting_seen'), 0)""",
        (bet_id, bet_id))[0]["c"]
```

- [ ] **Step 6: Write the recovery marker in the sighted branch**

In `arb/tracker.py`, inside the `for bet in active:` loop, the sighted branch currently begins at line 152 with `cand = sighted[bet["fingerprint"]]`. Insert the marker immediately after that line:

```python
            cand = sighted[bet["fingerprint"]]
            if _consecutive_misses(store, bet["id"]):
                # recovery marker: resets the derived miss counter (Task 1)
                store.insert("bet_events", {
                    "bet_id": bet["id"], "created_at": now.isoformat(),
                    "kind": "sighting_seen",
                    "payload_json": json.dumps({"poll_id": poll_id})})
```

- [ ] **Step 7: Replace the close-on-first-absence branch**

In `arb/tracker.py`, replace the entire `else:` branch (lines 181-194, beginning `leg_venues = {leg["venue"] ...}` and ending with the `lifetime_closed` insert) with:

```python
            leg_venues = {leg["venue"] for leg in json.loads(bet["legs_json"])}
            gap = [v for v in leg_venues if not venue_status.get(v, {}).get("ok")]
            store.insert("bet_events", {
                "bet_id": bet["id"], "created_at": now.isoformat(),
                "kind": "sighting_missed",
                "payload_json": json.dumps({"poll_id": poll_id, "venue_gap": gap})})
            if gap:
                store.insert("annotations", {
                    "created_at": now.isoformat(), "target_kind": "poll",
                    "target_id": str(poll_id), "kind": "venue-fetch-gap",
                    "detail": f"bet {bet['id']} carried; venues down: {gap}"})
            misses = _consecutive_misses(store, bet["id"])
            if misses >= int(cfg["tracker"]["dead_checks"]):
                # a bounded grace period, not an indefinite hold: a chronically
                # down venue carried bet 126 for 300 polls (2026-07-28)
                store.insert("bet_events", {
                    "bet_id": bet["id"], "created_at": now.isoformat(),
                    "kind": "lifetime_closed",
                    "payload_json": json.dumps({
                        "reason": "venue-gap-timeout" if gap else "absent-n-polls",
                        "misses": misses, "poll_id": poll_id})})
```

- [ ] **Step 8: Run the new tests to verify they pass**

Run: `pytest tests/test_tracker.py -k "missed or dead_checks or reappearance_resets or venue_gap_carry" -v`
Expected: 4 PASSED.

- [ ] **Step 9: Run the full suite**

Run: `pytest -q`
Expected: all pass. If a pre-existing test asserts the literal string `"absent-one-poll"`, update that assertion to `"absent-n-polls"` and adjust its poll count to `dead_checks` — the reason string changed by design, and this task's tests pin the new behaviour.

- [ ] **Step 10: Confirm the report is unchanged**

Run: `arb report --daily 2026-07-28 | diff /tmp/report-baseline.txt -`
Expected: no output. This is a read-time report over historical rows; no already-written row changed.

- [ ] **Step 11: Commit**

```bash
git add arb/tracker.py arb/config.py config.yaml tests/test_tracker.py && git commit -m "$(cat <<'EOF'
Tracker: one missed sighting is noise, not death

absent-one-poll closed a lifetime on the first absence. Measured against the
production DB, 19.5% of watch-lane closures came back within 1-2 polls (14.6%
at the very next poll) - bookkeeping deaths, and each reappearance minted a new
flat_bets row.

dead_checks (3) requires consecutive misses. "Consecutive" is derived from an
appended sighting_seen recovery marker, never a counter column (rule 9).

Same branch bounds the venue-fetch-gap carry: bet 126 was held open 300 polls
by a chronically-down sx.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Make the reappearance cap honest and cadence-proof

`config.yaml:100` documents `max_reappearances: 10` as an `# observability bound, logs only`. It is not. `arb/tracker.py:265-272` calls `_lot()` and `continue`s — **the flag is never booked.** 73 `reappearance-storm` annotations exist in production; 72 fired on a single fingerprint on 2026-07-15.

The cap counts `flat_bets` rows per rolling 24h, which scales linearly with poll rate. At 20s cadence the same physical arb produces 3x the rows against an unchanged cap, so a cadence change would silently begin eating exactly the recurring cross-venue positions that most deserve measurement. `arb/invariants.py:60-70` cannot catch this, because the suppressing code writes the very annotation the invariant looks for.

**Files:**
- Modify: `arb/tracker.py:264-265`
- Modify: `config.yaml` (add `tracker.reappearance_ref_interval_secs`; the misleading comment was already corrected in Task 1 Step 1)
- Modify: `arb/config.py` (schema entry)
- Test: `tests/test_tracker.py`

**Interfaces:**
- Produces: `arb.tracker.reappearance_cap(cfg) -> int`
- Consumes: `cfg["tracker"]["max_reappearances"]`, `cfg["tracker"]["reappearance_ref_interval_secs"]`, `cfg["watch_interval_secs"]`

- [ ] **Step 1: Add the reference-interval config key**

In `config.yaml`, add one line to the `tracker:` block edited in Task 1:

```yaml
  reappearance_ref_interval_secs: 60   # max_reappearances is a rows-per-24h RATE, and rows
                                       #   scale with poll rate. The cap is defined at this
                                       #   reference cadence and scaled by the configured one,
                                       #   so dropping watch_interval_secs cannot silently
                                       #   start suppressing flags.
```

- [ ] **Step 2: Declare it in the schema validator**

In `arb/config.py`, extend the `tracker` entry edited in Task 1 Step 2 to:

```python
    "tracker": {"terminal_mute_grace_mins": NUM, "max_reappearances": NUM,
                "dead_checks": NUM, "reappearance_ref_interval_secs": NUM},
```

- [ ] **Step 3: Write the failing test**

Append to `tests/test_tracker.py`:

```python
def test_reappearance_cap_scales_with_cadence(cfg):
    """The cap is a rows/24h rate. Tripling the poll rate triples row production;
    an unscaled cap would start suppressing flags with no code change."""
    from arb.tracker import reappearance_cap
    c = {**cfg, "watch_interval_secs": 60}
    assert reappearance_cap(c) == 10
    assert reappearance_cap({**c, "watch_interval_secs": 20}) == 30
    assert reappearance_cap({**c, "watch_interval_secs": 120}) == 5
    # never zero: a pathological interval must not suppress every flag
    assert reappearance_cap({**c, "watch_interval_secs": 100000}) == 1


def test_reappearance_storm_suppresses_booking(cfg, tmp_path):
    """Pins the behaviour the config comment used to deny: this is a booking kill,
    not a log line. 72 flags were suppressed on one fingerprint on 2026-07-15."""
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    fp = None
    for i in range(reappearance_cap_rows := 12):
        t = NOW + timedelta(seconds=60 * i)
        pid = _poll(store, "watch", t)
        process_poll(cfg, store, pid, "watch", [opp], VENUE_OK, now=t)
        rows = store.query("SELECT id, fingerprint FROM flat_bets ORDER BY id")
        fp = rows[-1]["fingerprint"]
        # force a fresh lifetime each round so the next poll books a new row
        store.insert("bet_events", {
            "bet_id": rows[-1]["id"], "created_at": t.isoformat(),
            "kind": "lifetime_closed",
            "payload_json": json.dumps({"reason": "test-forced"})})

    from arb.tracker import reappearance_cap
    booked = store.query("SELECT COUNT(*) c FROM flat_bets WHERE fingerprint=?",
                         (fp,))[0]["c"]
    assert booked == reappearance_cap(cfg)
    assert store.query(
        "SELECT COUNT(*) c FROM left_on_table WHERE reason='reappearance-storm'"
    )[0]["c"] == reappearance_cap_rows - reappearance_cap(cfg)
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `pytest tests/test_tracker.py -k "reappearance_cap or storm_suppresses" -v`
Expected: 2 FAILED — `ImportError: cannot import name 'reappearance_cap' from 'arb.tracker'`.

- [ ] **Step 5: Implement the scaled cap**

In `arb/tracker.py`, insert after `_consecutive_misses` (added in Task 1):

```python
def reappearance_cap(cfg) -> int:
    """max_reappearances is a rows-per-24h RATE, not a count, and rows scale with
    poll rate. Scale the cap by the configured cadence so a cadence change cannot
    silently start suppressing flags. NB this cap SUPPRESSES BOOKING (H6) — it is
    not an observability counter, whatever the old config comment said."""
    ref = float(cfg["tracker"]["reappearance_ref_interval_secs"])
    interval = float(cfg["watch_interval_secs"])
    return max(1, round(int(cfg["tracker"]["max_reappearances"]) * ref / interval))
```

- [ ] **Step 6: Use it at the call site**

In `arb/tracker.py`, replace line 264:

```python
        cap = int(cfg["tracker"]["max_reappearances"])
```

with:

```python
        cap = reappearance_cap(cfg)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `pytest tests/test_tracker.py -k "reappearance_cap or storm_suppresses" -v`
Expected: 2 PASSED.

- [ ] **Step 8: Run the full suite and confirm the report**

Run: `pytest -q && arb report --daily 2026-07-28 | diff /tmp/report-baseline.txt -`
Expected: all tests pass, no diff output.

- [ ] **Step 9: Commit**

```bash
git add arb/tracker.py arb/config.py config.yaml tests/test_tracker.py && git commit -m "$(cat <<'EOF'
Tracker: the reappearance cap is a rate, and it kills bookings

config said "observability bound, logs only". tracker.py LOTs the candidate and
continues - the flag is never booked. 73 storm annotations in production, 72 on
one fingerprint.

The cap counts rows/24h, which scale with poll rate, so dropping the cadence
would silently start suppressing recurring cross-venue positions. Scale it by
the configured interval against a declared reference cadence, and pin the
suppression behaviour in a test so it can never again be believed to be a log
line.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Stop the mapped lane confirming bets in the poll that created them

`arb/cycle.py:90-91` — `persist_poll` already calls `process_poll`. `arb/nonsports/mapped.py:225-226` calls it **again on the same `poll_id`**. The first pass books the bet; the second sees the fresh bet as active with its fingerprint sighted and writes a `confirmed` event.

Production proof: `bet 46` has `poll_id=10870` and `conf_poll=10870`, with `confirmed_at` (`19:04:48`) *preceding* `created_at` (`19:04:51`). **All 11 mapped confirms — the lane's "100% confirm rate" — are same-poll artifacts with zero elapsed time and no re-quote.**

The second call also passes `{"kalshi": {"ok": True}}` only, so polymarket reads as not-ok and PM-legged mapped bets are carried by the venue-gap rule instead of closing. Deleting the call fixes both, because `persist_poll` already receives the correct two-venue status dict.

**Files:**
- Modify: `arb/nonsports/mapped.py:189` (unused import) and `arb/nonsports/mapped.py:225-226`
- Test: `tests/test_mapped.py`

**Interfaces:** None changed. `run_mapped(cfg, store, now=None) -> dict` keeps its signature and return keys.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mapped.py`. Read the top of that file first for its existing fixtures; this test needs only `run_mapped`, a `Store`, and the module's `cfg` fixture.

```python
def test_mapped_runs_the_tracker_exactly_once_per_poll(cfg, tmp_path, monkeypatch):
    """A confirm is a SECOND sighting on a LATER poll. mapped called process_poll
    twice on one poll_id, so the bet booked by the first pass was seen as
    active-and-sighted by the second and got a confirmed event with zero elapsed
    time — confirmed_at preceding created_at (bets 46, 52 in production).

    Counting the calls reproduces the defect deterministically without having to
    construct an Opportunity that actually books."""
    import arb.tracker
    from arb.store import Store
    from arb.nonsports import mapped as M
    store = Store(tmp_path / "t.sqlite")

    calls = []
    real = arb.tracker.process_poll

    def counting(cfg_, store_, poll_id, poll_kind, opps, venue_status, now=None):
        calls.append((poll_id, dict(venue_status)))
        return real(cfg_, store_, poll_id, poll_kind, opps, venue_status, now=now)

    monkeypatch.setattr(arb.tracker, "process_poll", counting)

    entry = {"event_key": "nonsports:test:pair", "kalshi": "KXTEST",
             "polymarket": "0xtest"}
    monkeypatch.setattr(M, "load_map", lambda: [entry])
    monkeypatch.setattr(M, "kalshi_pair_quotes", lambda cfg_, e, now: [])
    monkeypatch.setattr(M, "pm_pair_tokens", lambda cfg_, e: [])
    monkeypatch.setattr(M, "detect_pair", lambda cfg_, e, qs: [])

    M.run_mapped(cfg, store, now=NOW)

    assert len(calls) == 1, f"process_poll ran {len(calls)}x on one poll"
    # and the surviving call carries BOTH venues: the deleted second call passed
    # kalshi-only, so PM-legged mapped bets were carried by the venue-gap rule
    assert set(calls[0][1]) == {"kalshi", "polymarket"}
    assert len(store.query("SELECT id FROM polls")) == 1


def test_mapped_ledger_has_no_same_poll_confirms(cfg, tmp_path, monkeypatch):
    """The observable consequence, asserted directly against the ledger."""
    from arb.store import Store
    from arb.nonsports import mapped as M
    store = Store(tmp_path / "t.sqlite")
    monkeypatch.setattr(M, "load_map", lambda: [])
    M.run_mapped(cfg, store, now=NOW)
    assert store.query(
        """SELECT be.id FROM bet_events be JOIN flat_bets fb ON fb.id = be.bet_id
           WHERE be.kind='confirmed'
             AND json_extract(be.payload_json, '$.poll_id') = fb.poll_id""") == []
```

Notes for the implementer:
- `monkeypatch.setattr(arb.tracker, "process_poll", ...)` patches the module attribute, and both call sites import it lazily inside their functions (`arb/cycle.py:90`, `arb/nonsports/mapped.py:189`), so both resolve to the patched object. Patching `M.process_poll` instead would only catch one of them — that is the whole point of the test.
- If `tests/test_mapped.py` does not already define `NOW`, add `NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)` near the top alongside the existing imports.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_mapped.py -k "runs_the_tracker_exactly_once or no_same_poll_confirms" -v`
Expected: `test_mapped_runs_the_tracker_exactly_once_per_poll` FAILED with `AssertionError: process_poll ran 2x on one poll`. `test_mapped_ledger_has_no_same_poll_confirms` passes vacuously with an empty map — it is the regression guard, not the reproduction.

Cross-check the reproduction by inspection: `grep -n "process_poll" arb/nonsports/mapped.py` must show the import at :189 and the call at :225, and `grep -n "process_poll" arb/cycle.py` must show the call inside `persist_poll` at :90.

- [ ] **Step 3: Delete the second call**

In `arb/nonsports/mapped.py`, delete these two lines (currently 225-226):

```python
    process_poll(cfg, store, poll_id, "mapped", all_opps,
                 {"kalshi": {"ok": True}}, now=now)
```

and delete the now-unused import on line 189:

```python
    from arb.tracker import process_poll
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_mapped.py -v`
Expected: all PASSED.

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add arb/nonsports/mapped.py tests/test_mapped.py && git commit -m "$(cat <<'EOF'
Mapped: stop confirming bets in the poll that created them

persist_poll already calls process_poll (cycle.py:90). mapped called it a second
time on the same poll_id, so the fresh bet was seen as active-and-sighted and got
a confirmed event with zero elapsed time - confirmed_at preceding created_at
(bets 46, 52). All 11 mapped confirms, and the lane's headline 100% confirm rate,
were this artifact.

The second call also passed kalshi-only venue status, so PM-legged mapped bets
were carried by the venue-gap rule instead of closing. persist_poll already
passes the correct two-venue dict.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Turn on the `verify` lane

`arb/lanes/verify.py` is complete, `cmd_verify` is wired at `arb/commands.py:420`, `verify` is a registered CLI subcommand at `arb/cli.py:66`, and `arb/heartbeat.py:118-119` already has its cadence branch. It is absent from `supervisor.processes`, so that branch is unreachable and the lane has **never run**: the production DB holds exactly one `verify` annotation, from 2026-07-08, verdict `unverifiable`.

It is the only thing in the repo that measures sub-cadence arb lifetime, and it costs zero Odds API credits — `arb/lanes/verify.py` hits Kalshi orderbook and PM books only (rule 2 safe).

**Files:**
- Modify: `config.yaml` (`supervisor.processes`, `supervisor.commands`, `supervisor.max_child_runtime_secs`)
- Test: `tests/test_ops.py`

**Interfaces:**
- Consumes: `arb.heartbeat.lane_interval(cfg, "verify")`, already implemented, returns `cfg["verify"]["poll_secs"]` (10).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ops.py`:

```python
def test_verify_lane_is_supervised(cfg):
    """verify is implemented, wired, and has a cadence branch, but was never in
    supervisor.processes - one annotation in 21 days. It is the only sub-cadence
    arb-lifetime measurement in the repo and costs zero Odds API credits."""
    from arb.heartbeat import lane_interval
    procs = cfg["supervisor"]["processes"]
    assert procs.get("verify") is True
    assert cfg["supervisor"]["commands"]["verify"] == "verify --once"
    assert lane_interval(cfg, "verify") == float(cfg["verify"]["poll_secs"])


def test_every_supervised_process_has_a_command(cfg):
    """A process enabled with no command entry KeyErrors the supervisor loop."""
    for name, on in cfg["supervisor"]["processes"].items():
        if on:
            assert name in cfg["supervisor"]["commands"], name
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_ops.py -k "verify_lane_is_supervised or every_supervised" -v`
Expected: `test_verify_lane_is_supervised` FAILED on `assert procs.get("verify") is True` (it is `None`). `test_every_supervised_process_has_a_command` PASSES already — it is a guard for the change about to be made.

- [ ] **Step 3: Enable the lane**

In `config.yaml`, replace the `supervisor:` `processes` and `commands` blocks (lines 249-254):

```yaml
  processes: { watch: true, live: true,    # add lanes deliberately, one at a time
               negrisk: true, political: true,    # non-sports pivot (plan Phase 2)
               mapped: true,                      # adjudicated cross-venue pairs
               verify: true }                     # 10s re-quote of fresh locked flags.
                                                  #   Implemented since Phase 7 and never
                                                  #   run: 1 annotation in 21 days. Keyless
                                                  #   Kalshi/PM reads only (rule 2 safe).
  commands:  { watch: "watch --once", live: "live --once",
               negrisk: "negrisk-scan --once", political: "political --once",
               mapped: "mapped --once", verify: "verify --once" }
```

- [ ] **Step 4: Bound its runtime**

In `config.yaml`, extend `supervisor.max_child_runtime_secs` (line 255) to:

```yaml
  max_child_runtime_secs: { default: 600, settle: 1800, live: 120, verify: 30 }
                                    # per-process; settle+reconcile runs long;
                                    #   a live cycle past 120s is force-restarted.
                                    #   verify polls at 10s — a 30s child is already hung
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_ops.py -k "verify_lane_is_supervised or every_supervised" -v`
Expected: 2 PASSED.

- [ ] **Step 6: Dry-run the lane once by hand before it is supervised**

Run: `arb verify --once`
Expected: prints `verify: re-quoted N fresh locked flags` and exits 0. If it raises, **stop** — do not commit a lane that crashes; diagnose with `superpowers:systematic-debugging` first.

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add config.yaml tests/test_ops.py && git commit -m "$(cat <<'EOF'
Supervisor: actually run the verify lane

arb/lanes/verify.py is complete, cmd_verify is wired, and heartbeat has its
cadence branch - but verify was never in supervisor.processes, so the branch was
unreachable. One annotation in 21 days, verdict unverifiable.

It is the only sub-cadence arb-lifetime measurement in the repo and reads only
keyless Kalshi/PM endpoints (rule 2 safe). Bounded at 30s per child.

Also pins that every enabled process has a command entry.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

**Phase 1 gate.** Run `pytest -q` and `arb report --daily 2026-07-28 | diff /tmp/report-baseline.txt -`. Both must be clean. The ledger now keeps flickering arbs alive, cannot silently suppress flags at a new cadence, no longer fakes mapped confirms, and measures sub-cadence lifetimes for the first time. **Stopping here is a coherent, shippable outcome.**

---

# Phase 2 — Scheduling honesty

Zero new network requests. Measured: **55% of watch polls currently run with another lane's poll in flight**, costing kalshi +28% and sx **+75%** median latency (+241% with two overlapping). The 2026-07-14 stagger fix has worn off because it is applied once at boot and the schedule is anchored to the previous spawn, not to a grid.

### Task 5: Anchor lane scheduling to an absolute grid

Two defects in `arb/heartbeat.py`:

1. `arb/heartbeat.py:190` sets `next_run[name] = now + lane_interval(cfg, name)`. The loop's `time.sleep(2)` (`:219`) makes each cycle overshoot by 0–2s, so every lane's true period is `interval + drift` and lanes sweep through each other. Measured on 2026-07-28: the nearest `live` poll to each `watch` poll, sampled hourly, was `+3, +1, +5, -8, -3, -1, -5, -5, -6, +1, ...` seconds — never staggered, all day.
2. `arb/heartbeat.py:101-108` spreads lanes by `i * stagger_secs`. With 5 enabled lanes and `stagger_secs: 15`, offsets are 0/15/30/45/**60** — and `watch_interval_secs` is 60, so lane 5 boots in exact lockstep with lane 1. `stagger_secs: 15` and `watch_interval_secs: 60` cannot both hold at 5 lanes; Task 4 made it 6.

Fix: give each lane a phase offset spread evenly *inside its own period*, and anchor `next_run` to an absolute grid so phase can never drift.

**Files:**
- Modify: `arb/heartbeat.py:101-108` (`initial_next_run`), `arb/heartbeat.py:186-190` (the spawn block)
- Test: `tests/test_ops.py`

**Interfaces:**
- Produces: `arb.heartbeat.lane_offsets(cfg) -> dict[str, float]`
- Produces: `arb.heartbeat.next_grid_run(now: float, interval: float, offset: float) -> float`
- Replaces: `arb.heartbeat.initial_next_run(cfg, now_mono)` keeps its name and signature but is reimplemented in terms of the two above.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ops.py`:

```python
def test_next_grid_run_is_strictly_future_and_drift_free():
    """Anchoring to now+interval accumulates the supervisor's 2s tick overshoot;
    an absolute grid cannot drift no matter how late the tick arrives."""
    from arb.heartbeat import next_grid_run
    assert next_grid_run(now=0.0, interval=60.0, offset=0.0) == 60.0
    assert next_grid_run(now=59.9, interval=60.0, offset=0.0) == 60.0
    assert next_grid_run(now=60.0, interval=60.0, offset=0.0) == 120.0
    # a late tick still lands on the grid, it does not push the phase out
    assert next_grid_run(now=119.5, interval=60.0, offset=0.0) == 120.0
    assert next_grid_run(now=0.0, interval=60.0, offset=12.0) == 12.0
    assert next_grid_run(now=12.0, interval=60.0, offset=12.0) == 72.0


def test_lane_offsets_never_collide_within_a_period(cfg):
    """stagger_secs 15 x 5 lanes = 60s = watch_interval_secs, so lane 5 booted in
    exact lockstep with lane 1. Spread inside each lane's own period instead."""
    from arb.heartbeat import lane_offsets, lane_interval
    offs = lane_offsets(cfg)
    enabled = [n for n, on in cfg["supervisor"]["processes"].items() if on]
    assert set(offs) == set(enabled)
    for name in enabled:
        assert 0 <= offs[name] < lane_interval(cfg, name)
    assert len(set(offs.values())) == len(enabled), offs


def test_watch_and_live_are_never_scheduled_simultaneously(cfg):
    """live.cadence_secs 20 divides watch_interval_secs 60 exactly, so a shared
    phase is permanent, not transient. Simulate an hour of grid slots."""
    from arb.heartbeat import lane_offsets, lane_interval, next_grid_run
    offs = lane_offsets(cfg)

    def slots(name, horizon=3600.0):
        interval, off, t, out = lane_interval(cfg, name), offs[name], 0.0, []
        while t < horizon:
            t = next_grid_run(t, interval, off)
            out.append(round(t, 3))
        return out

    watch, live = set(slots("watch")), set(slots("live"))
    assert not (watch & live), sorted(watch & live)[:5]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_ops.py -k "grid_run or lane_offsets or simultaneously" -v`
Expected: 3 FAILED — `ImportError: cannot import name 'next_grid_run' from 'arb.heartbeat'`.

- [ ] **Step 3: Add `math` to the imports**

In `arb/heartbeat.py`, add `import math` to the standard-library import block at the top of the file, keeping alphabetical order with the existing imports.

- [ ] **Step 4: Replace `initial_next_run` with grid scheduling**

In `arb/heartbeat.py`, replace `initial_next_run` (lines 101-108) with:

```python
def lane_offsets(cfg) -> dict[str, float]:
    """Phase offsets spread evenly INSIDE each lane's own period. A flat boot
    stagger aliases — 5 lanes x stagger_secs 15 = 60s = watch_interval_secs, so
    lane 5 landed on lane 1 — and it decayed anyway, because next_run was
    anchored to the previous spawn rather than to a grid (2026-07-28: 55% of
    watch polls ran with another lane in flight; sx p50 latency +75%)."""
    enabled = [n for n, on in cfg["supervisor"]["processes"].items() if on]
    n = max(1, len(enabled))
    offsets = {}
    for i, name in enumerate(enabled):
        interval = lane_interval(cfg, name)
        offsets[name] = interval * i / n
        want = float(cfg["supervisor"]["stagger_secs"])
        if interval / n < want:
            log.warning("lane %s phase separation %.1fs < stagger_secs %s — "
                        "cadence too fast for %d lanes on one IP",
                        interval / n, want, n)
    return offsets


def next_grid_run(now: float, interval: float, offset: float) -> float:
    """The next absolute-grid slot strictly after `now`. Phase is a property of
    the grid, so a late supervisor tick can never push a lane out of alignment."""
    return (math.floor((now - offset) / interval) + 1) * interval + offset


def initial_next_run(cfg, now_mono: float) -> dict[str, float]:
    """Boot-time schedule: first grid slot per lane."""
    offsets = lane_offsets(cfg)
    return {name: next_grid_run(now_mono, lane_interval(cfg, name), off)
            for name, off in offsets.items()}
```

- [ ] **Step 5: Anchor the re-arm to the grid**

In `arb/heartbeat.py`, the spawn block currently ends at line 190 with `next_run[name] = now + lane_interval(cfg, name)`. Replace that single line with:

```python
                    next_run[name] = next_grid_run(
                        now, lane_interval(cfg, name), offsets[name])
```

Then, in the same function, immediately after the line that initialises `next_run` from `initial_next_run(...)`, add:

```python
    offsets = lane_offsets(cfg)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_ops.py -k "grid_run or lane_offsets or simultaneously" -v`
Expected: 3 PASSED.

- [ ] **Step 7: Verify the supervisor still starts and stops cleanly**

Run: `arb supervisor --once`
Expected: exits 0 after spawning and terminating one round of children. Watch for a `phase separation` warning — with 6 lanes and `stagger_secs: 15`, `live` (20s / 6 = 3.3s) will warn. That warning is correct and is the point of the check; note it and continue.

- [ ] **Step 8: Run the full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add arb/heartbeat.py tests/test_ops.py && git commit -m "$(cat <<'EOF'
Supervisor: anchor lanes to an absolute grid

next_run was set to now+interval, so the loop's 2s tick overshoot accumulated
and lanes swept through each other. Boot stagger was applied once and decayed.
Measured 2026-07-28: 55% of watch polls ran with another lane in flight, costing
kalshi +28% and sx +75% p50 latency (+241% with two).

Also fixes the aliasing: 5 lanes x stagger_secs 15 = 60s = watch_interval_secs,
so lane 5 booted in exact lockstep with lane 1, and live's 20s divides 60 exactly
so the collision was permanent. Offsets now spread evenly inside each lane's own
period, and a too-tight separation warns instead of silently colliding.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Alarm on per-lane silence

`negrisk` and `political` were dark from 2026-07-17 to 2026-07-25 — **zero polls, a 9.7-day gap** — while `watch`, `live`, and `mapped` kept running, and nothing reported it. `heartbeat_stale_polls` covers the supervisor beat as a whole, not per-lane silence.

Overall uptime since 2026-07-25 is **82%**: 56 gaps over 5 minutes, 19.9 dead hours out of 113. The dead time is inverted against yield — hour 22 is 37.7% dead, hour 21 is 27.6%, hour 19 is 24.7%, while hours 14/15/06 are 0.0% dead and produce nothing. Hours 19–22 hold **73% of all ARB flags ever**. This task does not fix the outages (cause unknown, see Task 15), it makes them impossible to miss.

**Files:**
- Create: nothing — extend `arb/heartbeat.py`
- Modify: `arb/heartbeat.py` (add `stale_lanes`, call it in the supervisor loop)
- Modify: `arb/commands.py` (surface it in `cmd_status`)
- Test: `tests/test_ops.py`

**Interfaces:**
- Produces: `arb.heartbeat.stale_lanes(cfg, store, now_utc: datetime) -> list[tuple[str, float]]` — pairs of `(lane_name, age_secs)` for enabled lanes whose newest poll is older than `heartbeat_stale_polls x lane_interval`.
- Produces: `arb.heartbeat.POLL_KIND: dict[str, str]` — supervisor process name to `polls.kind`. Only lanes that write `polls` rows appear; `verify` does not.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ops.py`:

```python
def test_stale_lanes_flags_a_silent_lane(cfg, tmp_path):
    """negrisk and political were dark for 9.7 days in July 2026 and nothing said
    so: the supervisor beat covers the supervisor, not each lane."""
    from arb.heartbeat import stale_lanes
    from arb.store import Store
    store = Store(tmp_path / "t.sqlite")
    fresh = NOW - timedelta(seconds=30)
    store.insert("polls", {"started_at": fresh.isoformat(), "kind": "watch",
                           "n_quotes": 1, "n_opps": 0})
    store.insert("polls", {"started_at": (NOW - timedelta(days=9)).isoformat(),
                           "kind": "negrisk", "n_quotes": 1, "n_opps": 0})

    stale = dict(stale_lanes(cfg, store, NOW))
    assert "negrisk" in stale
    assert stale["negrisk"] > 9 * 86400 - 60
    assert "watch" not in stale


def test_stale_lanes_ignores_lanes_that_never_ran(cfg, tmp_path):
    """A lane with no polls at all is a cold start, not a silence: alarming on it
    would fire on every fresh DB."""
    from arb.heartbeat import stale_lanes
    from arb.store import Store
    assert stale_lanes(cfg, Store(tmp_path / "t.sqlite"), NOW) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_ops.py -k "stale_lanes" -v`
Expected: 2 FAILED — `ImportError: cannot import name 'stale_lanes' from 'arb.heartbeat'`.

- [ ] **Step 3: Implement `stale_lanes`**

In `arb/heartbeat.py`, insert after `lane_interval` (which ends at line 122):

```python
# supervisor process name -> polls.kind. verify writes annotations, not polls.
POLL_KIND = {"watch": "watch", "live": "live", "negrisk": "negrisk",
             "political": "political", "mapped": "mapped"}


def stale_lanes(cfg, store, now_utc: datetime) -> list[tuple[str, float]]:
    """Enabled lanes whose newest poll is older than heartbeat_stale_polls x
    their cadence. A lane that has NEVER polled is a cold start, not a silence.
    negrisk+political were dark 9.7 days (2026-07-17..25) unreported."""
    mult = float(cfg["heartbeat_stale_polls"])
    out = []
    for name, on in cfg["supervisor"]["processes"].items():
        kind = POLL_KIND.get(name)
        if not on or kind is None:
            continue
        last = store.query("SELECT MAX(started_at) m FROM polls WHERE kind=?",
                           (kind,))[0]["m"]
        if last is None:
            continue
        age = (now_utc - datetime.fromisoformat(last)).total_seconds()
        if age > mult * lane_interval(cfg, name):
            out.append((name, age))
    return out
```

If `datetime` is not already imported in `arb/heartbeat.py`, add it to the existing `from datetime import ...` line.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_ops.py -k "stale_lanes" -v`
Expected: 2 PASSED.

- [ ] **Step 5: Alarm from the supervisor loop**

In `arb/heartbeat.py`, inside the supervisor's main `while` loop, immediately before the `time.sleep(2)` at line 219, insert:

```python
            for name, age in stale_lanes(cfg, store, datetime.now(timezone.utc)):
                log.critical("lane %s silent for %.0fs (%.1f h) — no poll rows",
                             name, age, age / 3600)
```

If `store` is not in scope in `run_supervisor`, open one from the `db` argument the function already receives, using the same `Store(...)` construction `arb/commands.py` uses. If `timezone` is not imported, add it.

- [ ] **Step 6: Surface it in `arb status`**

In `arb/commands.py`, find `cmd_status` and add, immediately before its `return 0`:

```python
    from arb.heartbeat import stale_lanes
    for name, age in stale_lanes(cfg, store, datetime.now(timezone.utc)):
        console.print(f"[red]LANE SILENT[/] {name}: no poll for {age / 3600:.1f} h")
```

- [ ] **Step 7: Verify status runs against the real DB**

Run: `arb status`
Expected: exits 0. Against the production DB it should print nothing new if all lanes are current; if a lane really is silent it prints a red line, which is the intended behaviour, not a failure.

- [ ] **Step 8: Run the full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add arb/heartbeat.py arb/commands.py tests/test_ops.py && git commit -m "$(cat <<'EOF'
Supervisor: alarm on per-lane silence

negrisk and political were dark 2026-07-17..25 - zero polls, a 9.7-day gap -
while watch/live/mapped kept running, and nothing reported it.
heartbeat_stale_polls covers the supervisor beat, not each lane.

stale_lanes compares each enabled lane's newest poll against its own cadence.
A lane that has never polled is a cold start, not a silence. Logged CRITICAL by
the supervisor and printed by arb status.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Time-of-day cadence windows (mechanism only)

Measured watch-lane flags per 1,000 polls by NY hour: **hour 20 → 482, hour 19 → 294, hour 21 → 281** versus **hours 04–08 → 1,816 polls producing 3 flags**. Hours 19–22 hold 73% of all ARB flags. `negrisk` and `mapped` are perfectly flat across all 24 hours — non-sports markets have no time-of-day — and they are the lanes running at 300s while `live` (2.2 flags/1k polls) runs at 20s.

**This task ships the mechanism with a default that changes nothing.** Choosing the actual windows is a policy decision with `PREREGISTRATION.md` implications and belongs to the operator, not to this plan.

**Files:**
- Modify: `arb/heartbeat.py:111-122` (`lane_interval`)
- Modify: `config.yaml` (`supervisor.cadence_windows`, empty)
- Modify: `arb/config.py` (schema entry)
- Test: `tests/test_ops.py`

**Interfaces:**
- Changes: `arb.heartbeat.lane_interval(cfg, name, now_ny: datetime | None = None) -> float`. The third parameter is optional and defaults to `None`, so every existing call site (`arb/heartbeat.py:190`, and the new ones in Tasks 5 and 6) keeps working unchanged and keeps getting the base cadence.
- Consumes: `cfg["supervisor"]["cadence_windows"]` — a map of lane name to a list of `[start_hour, end_hour, interval_secs]` triples, hours in `America/New_York`, `start_hour` inclusive and `end_hour` exclusive. An empty map (the default) is a no-op.

- [ ] **Step 1: Add the config key**

In `config.yaml`, add to the `supervisor:` block, after `shape_time`:

```yaml
  cadence_windows: {}               # optional per-lane time-of-day cadence, NY hours:
                                    #   { watch: [[19, 23, 20]] } polls watch every 20s
                                    #   from 19:00 to 22:59 and at watch_interval_secs
                                    #   otherwise. Triples are [start_hr, end_hr, secs],
                                    #   start inclusive, end exclusive; first match wins.
                                    #   EMPTY BY DEFAULT — measured yield (hr20: 482 flags
                                    #   /1k polls vs hrs 04-08: 3 flags in 1,816 polls) says
                                    #   this should be used, but the numbers are a
                                    #   pre-registered policy decision, not a code default.
```

- [ ] **Step 2: Declare it in the schema validator**

In `arb/config.py`, extend the `supervisor` entry (line 122) to include `"cadence_windows": MAP(LIST)`:

```python
    "supervisor": {"processes": MAP(BOOL), "commands": MAP(STR),
                   "max_child_runtime_secs": MAP(NUM),
                   "schedule_catchup_grace_mins": NUM, "stagger_secs": NUM,
                   "shape_time": STR, "cadence_windows": MAP(LIST)},
```

- [ ] **Step 3: Write the failing test**

Append to `tests/test_ops.py`:

```python
def test_cadence_windows_default_to_a_no_op(cfg):
    """Ships as a mechanism. The numbers are a pre-registered policy decision."""
    from arb.heartbeat import lane_interval
    assert cfg["supervisor"]["cadence_windows"] == {}
    evening = datetime(2026, 7, 28, 20, 0)
    assert lane_interval(cfg, "watch", evening) == float(cfg["watch_interval_secs"])


def test_cadence_window_overrides_inside_its_hours(cfg):
    from arb.heartbeat import lane_interval
    c = {**cfg, "supervisor": {**cfg["supervisor"],
                               "cadence_windows": {"watch": [[19, 23, 20]]}}}
    assert lane_interval(c, "watch", datetime(2026, 7, 28, 19, 0)) == 20.0
    assert lane_interval(c, "watch", datetime(2026, 7, 28, 22, 59)) == 20.0
    assert lane_interval(c, "watch", datetime(2026, 7, 28, 23, 0)) == 60.0
    assert lane_interval(c, "watch", datetime(2026, 7, 28, 5, 0)) == 60.0
    # a lane with no window is untouched
    assert lane_interval(c, "mapped", datetime(2026, 7, 28, 20, 0)) == \
        float(c["nonsports"]["watch_interval_secs"])


def test_lane_interval_without_a_clock_returns_the_base_cadence(cfg):
    """Every existing call site passes no clock and must keep the base cadence."""
    from arb.heartbeat import lane_interval
    c = {**cfg, "supervisor": {**cfg["supervisor"],
                               "cadence_windows": {"watch": [[0, 24, 5]]}}}
    assert lane_interval(c, "watch") == float(c["watch_interval_secs"])
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `pytest tests/test_ops.py -k "cadence_window" -v`
Expected: `test_cadence_windows_default_to_a_no_op` FAILS on `KeyError: 'cadence_windows'` if Step 1 was skipped, otherwise on the extra `lane_interval` argument; `test_cadence_window_overrides_inside_its_hours` FAILS with `TypeError: lane_interval() takes 2 positional arguments but 3 were given`.

- [ ] **Step 5: Implement the window lookup**

In `arb/heartbeat.py`, rename the existing `lane_interval` to `_base_lane_interval` (change only the `def` line; its body is unchanged), then add immediately below it:

```python
def lane_interval(cfg, name: str, now_ny: datetime | None = None) -> float:
    """Base cadence, optionally overridden by a time-of-day window. Callers that
    pass no clock always get the base — every pre-existing call site does.
    Measured yield is strongly diurnal for watch (hr20: 482 flags/1k polls vs
    3 flags in 1,816 polls across hrs 04-08) and perfectly flat for negrisk and
    mapped, which have no time-of-day at all."""
    base = _base_lane_interval(cfg, name)
    if now_ny is None:
        return base
    for start_h, end_h, secs in cfg["supervisor"]["cadence_windows"].get(name) or []:
        if int(start_h) <= now_ny.hour < int(end_h):
            return float(secs)
    return base
```

- [ ] **Step 6: Pass the clock from the supervisor loop**

In `arb/heartbeat.py`, the supervisor loop already computes `now_ny = datetime.now(NY)` each tick. In the spawn block edited in Task 5 Step 5, pass it through:

```python
                    next_run[name] = next_grid_run(
                        now, lane_interval(cfg, name, now_ny), offsets[name])
```

Leave `lane_offsets` and `stale_lanes` calling `lane_interval` without a clock — offsets are a fixed phase plan, and a silence alarm must not tighten just because the hour changed.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `pytest tests/test_ops.py -k "cadence_window or lane_interval_without" -v`
Expected: 3 PASSED.

- [ ] **Step 8: Run the full suite**

Run: `pytest -q`
Expected: all pass. In particular `test_verify_lane_is_supervised` from Task 4 must still pass — it calls `lane_interval(cfg, "verify")` with no clock.

- [ ] **Step 9: Commit**

```bash
git add arb/heartbeat.py arb/config.py config.yaml tests/test_ops.py && git commit -m "$(cat <<'EOF'
Supervisor: optional time-of-day cadence windows (default no-op)

Measured watch yield by NY hour: 482 flags/1k polls at hr20, 294 at hr19, 281 at
hr21 - against 3 flags across 1,816 polls in hrs 04-08. Hours 19-22 hold 73% of
all ARB flags. negrisk and mapped are perfectly flat across 24h.

Ships the mechanism only. cadence_windows is empty, so behaviour is byte-for-byte
unchanged; the numbers are a pre-registered policy decision for the operator.
lane_interval's clock argument is optional, so every existing call site keeps
the base cadence.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Stop the live lane spawning work with an empty shortlist

`live` runs 35,101 polls — more processes than every other lane combined — for **76 flags, 2.2 per 1,000 polls**. Of those, **14,658 polls in NY hours 02–11 have produced zero flags ever**, and 55 of its 76 lifetime flags land in a single hour (17).

`shortlist()` (`arb/lanes/live.py:24-56`) is pure SQL over stored quotes with no network. When it returns empty there is nothing to re-quote, yet the cycle still persists a poll row and runs the full tracker pass.

**Files:**
- Modify: `arb/lanes/live.py` (`run_live_cycle`)
- Test: `tests/test_live_lane.py`

**Interfaces:**
- Changes: `run_live_cycle` returns its existing dict with all keys present and zeroed when the shortlist is empty (`shortlist: []`, `identities: 0`, `requoted: 0`, `coverage: 0.0`, `flags: 0`, `rejects: 0`), plus a new key `skipped: True`. `cmd_live` (`arb/commands.py:310`) reads `s["shortlist"]`, `s["identities"]`, `s["requoted"]`, `s["coverage"]`, `s["flags"]`, `s["rejects"]`, `s["cycle_secs"]` — all must remain present.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live_lane.py`:

```python
def test_empty_shortlist_persists_nothing_and_fetches_nothing(cfg, tmp_path, monkeypatch):
    """live runs 35k polls for 76 flags; 14,658 of them in NY hours 02-11 produced
    zero flags ever. An empty shortlist means there is nothing in play."""
    from arb.lanes import live as L
    from arb.store import Store
    store = Store(tmp_path / "t.sqlite")

    def boom(*a, **kw):
        raise AssertionError("no network with an empty shortlist")

    monkeypatch.setattr(L, "shortlist", lambda cfg, store, now: [])
    for venue in L.REQUOTERS:
        monkeypatch.setitem(L.REQUOTERS, venue, boom)

    s = L.run_live_cycle(cfg, store, now=NOW)

    assert s["skipped"] is True
    assert s["shortlist"] == [] and s["flags"] == 0 and s["requoted"] == 0
    assert store.query("SELECT id FROM polls") == []
    for key in ("identities", "coverage", "rejects", "cycle_secs"):
        assert key in s, key
```

If `tests/test_live_lane.py` does not define `NOW` or a `cfg` fixture, copy them from `tests/test_ops.py`:

```python
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load(Path(__file__).resolve().parent.parent / "config.yaml")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_live_lane.py::test_empty_shortlist_persists_nothing_and_fetches_nothing -v`
Expected: FAIL on `KeyError: 'skipped'`, or on the poll row assertion.

- [ ] **Step 3: Return early on an empty shortlist**

In `arb/lanes/live.py`, in `run_live_cycle`, immediately after `events = shortlist(cfg, store, now)` insert:

```python
    if not events:
        # nothing in play: shortlist() is pure SQL over stored quotes, so this
        # costs no request and should persist no poll. 14,658 live polls in NY
        # hours 02-11 have produced zero flags ever (2026-07-28).
        return {"shortlist": [], "identities": 0, "requoted": 0, "coverage": 0.0,
                "flags": 0, "rejects": 0, "skipped": True,
                "cycle_secs": round(time.monotonic() - t0, 2)}
```

- [ ] **Step 4: Keep the key present on the normal path**

In `arb/lanes/live.py`, find the dict `run_live_cycle` returns at the end of the normal path and add `"skipped": False` to it, so callers never have to use `.get()`.

- [ ] **Step 5: Report the skip in the CLI**

In `arb/commands.py`, in `cmd_live`, insert immediately after the `run_live_cycle` call:

```python
    if s["skipped"]:
        console.print("[dim]live: empty shortlist — nothing in play, no fetch[/]")
        return 0
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `pytest tests/test_live_lane.py -v`
Expected: all PASSED.

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: all pass. `tests/test_live_cycle.py` and `tests/test_live_tracker.py` also exercise this lane — if either asserts on a poll row being written for an empty shortlist, that assertion encodes the old behaviour and should be updated to assert the skip.

- [ ] **Step 8: Commit**

```bash
git add arb/lanes/live.py arb/commands.py tests/test_live_lane.py && git commit -m "$(cat <<'EOF'
Live: an empty shortlist is a no-op, not a poll

The live lane runs 35,101 polls - more processes than every other lane combined -
for 76 flags (2.2 per 1k polls). 14,658 of those polls, all in NY hours 02-11,
have produced zero flags ever.

shortlist() is pure SQL over stored quotes, so an empty result is known before
any request. Return early instead of persisting a poll row and running a full
tracker pass over nothing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

**Phase 2 gate.** Run `pytest -q` and `arb report --daily 2026-07-28 | diff /tmp/report-baseline.txt -`. Then run the supervisor for at least 30 minutes and check that lanes no longer overlap:

```bash
sqlite3 data/arb.sqlite "SELECT kind, COUNT(*), ROUND(AVG(latency_ms)) FROM polls p JOIN poll_venues v ON v.poll_id=p.id WHERE p.started_at > datetime('now','-30 minutes') GROUP BY kind"
```

Expected: sx and kalshi mean latency at or below the no-overlap baseline (kalshi 605 ms, sx 5,390 ms). **Stopping here is a coherent, shippable outcome.**

---

# Phase 3 — Fetch efficiency and coverage

**Do not start this phase while the concurrent session still has `arb/venues/*.py`, `arb/matching.py`, or `data/aliases.json` modified.** Re-run Step 0.1 first.

Measured: **3,300 of 4,344 quotes per watch poll (76%) are fetched for sports that have produced zero flags in 21 days**, and `SELECT COUNT(*) FROM opportunities WHERE event_key=''` returns **0 of 57,690** — an unmatched quote has never once contributed to a flag. Actual request count is ~90–105 per cycle, all landing inside the first ~10s ≈ 8 req/s instantaneous against the ~2 req/s sustained ceiling.

### Task 9: Implement activity gating

`activity_lookahead_hours: 48` (`config.yaml:51`), `scan_breaks` (`:60`) and `break_windows` (`:61`) appear in exactly two places each: `config.yaml` and the validator at `arb/config.py:59,61`. **No code reads any of them.** `grep -rn activity_lookahead_hours arb/` returns only the schema line. Every adapter iterates the full `sports` list unconditionally: `arb/venues/kalshi.py:143`, `arb/venues/polymarket.py:139`, `arb/venues/sx.py:128`.

Consequence: with NBA out of season the system pulled **439,152 NBA quotes in July, 100% unmatched, 0 flags**.

The gate must be self-healing — a sport gated off stops producing quotes, so a naive "has recent quotes" rule would gate it off permanently. The rule is: fetch a sport if it has a *known fixture* inside the lookahead **OR** its most recent quote is older than a re-probe interval.

`scan_breaks` and `break_windows` are deleted rather than implemented: SPEC §13.1 promises a halftime re-scan for NBA and soccer, and both sports have produced zero flags in 21 days. Implementing a feature for two dead sports is work with no measurable payoff; the keys are removed so no future plan can again assert capability that does not exist.

**Files:**
- Create: `arb/activity.py`
- Create: `tests/test_activity.py`
- Modify: `arb/watch.py` (filter the sports list before fan-out)
- Modify: `arb/schema.py` (add `MIGRATION_7` index), `arb/migrations.py` (apply it)
- Modify: `config.yaml` (add `activity_reprobe_secs`; delete `scan_breaks`, `break_windows`)
- Modify: `arb/config.py` (schema: add the new key, delete the two dead ones)

**Interfaces:**
- Produces: `arb.activity.active_sports(cfg, store, now: datetime) -> list[str]` — a subset of `cfg["sports"]`, order preserved.
- Changes: `arb.watch.fetch_all_venues(cfg, store=None)` — when `store` is `None` the function behaves exactly as today (no gating), so any existing caller and every test keeps working.
- Consumes: `cfg["activity_lookahead_hours"]`, `cfg["activity_reprobe_secs"]`, `cfg["sports"]`.

- [ ] **Step 1: Add the re-probe config key and delete the dead ones**

In `config.yaml`, replace line 51 and delete lines 60-62:

```yaml
activity_lookahead_hours: 48        # sport ACTIVE iff a venue lists a fixture in window
activity_reprobe_secs: 3600         # ...OR its newest stored quote is older than this.
                                    #   Without the re-probe the gate is a trapdoor: a
                                    #   gated sport stops producing quotes, so it could
                                    #   never prove itself active again. In July 2026 the
                                    #   ungated loop pulled 439,152 out-of-season NBA
                                    #   quotes, 100% unmatched, 0 flags.
```

and delete these two lines entirely:

```yaml
scan_breaks: true
break_windows: { basketball_nba: [48, 15], soccer: [45, 15] }  # ...
```

(including the two continuation comment lines under `break_windows`).

- [ ] **Step 2: Update the schema validator**

In `arb/config.py`, change line 59 to add the new key:

```python
    "unreliable_start_venues": MAP(LIST), "activity_lookahead_hours": NUM,
    "activity_reprobe_secs": NUM,
```

and change line 61 to remove the two dead keys:

```python
    "odds_api_markets": MAP(STR),
```

- [ ] **Step 3: Add the supporting index**

In `arb/schema.py`, after `MIGRATION_6` (ends line 42), add:

```python
MIGRATION_7 = [
    # activity gating queries quotes by sport; without this it is a full scan of
    # a 13.7M-row table every cycle
    "CREATE INDEX IF NOT EXISTS idx_quotes_sport_start ON quotes(sport, start_utc)",
    "CREATE INDEX IF NOT EXISTS idx_quotes_sport_fetched ON quotes(sport, fetched_at)",
]
```

In `arb/migrations.py`, before the final `conn.commit()`, add:

```python
    if current < 7:
        for ddl in schema.MIGRATION_7:
            conn.execute(ddl)  # CREATE INDEX IF NOT EXISTS — idempotent
        conn.execute("INSERT INTO schema_version VALUES (7, datetime('now'))")
```

- [ ] **Step 4: Write the failing tests**

Create `tests/test_activity.py`:

```python
"""Activity gating (config: activity_lookahead_hours). 439,152 out-of-season NBA
quotes were pulled in July 2026, 100% unmatched, 0 flags."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arb.activity import active_sports
from arb.config import load
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


def _quote(store, sport, start, fetched):
    store.insert("quotes", {
        "poll_id": 1, "fetched_at": fetched.isoformat(), "event_key": "e",
        "category": "sports", "sport": sport, "venue": "kalshi", "book": "kalshi",
        "market_type": "WINNER", "line": None, "outcome": "a", "side": "YES",
        "raw_price": "0.5", "cost": "0.5", "fee_cost": "0", "depth_usd": "100",
        "ladder_json": None, "live": 0, "bid": "0.48", "volume_24h": "5",
        "start_utc": start.isoformat() if start else None,
        "start_grade": "verified", "venue_market_id": "x"})


def test_sport_with_a_fixture_in_the_window_is_active(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    _quote(store, "baseball_mlb", NOW + timedelta(hours=6), NOW)
    assert "baseball_mlb" in active_sports(cfg, store, NOW)


def test_sport_whose_only_fixtures_are_past_the_window_is_gated(cfg, tmp_path):
    """NBA out of season: fresh quotes, but no fixture inside the lookahead."""
    store = Store(tmp_path / "t.sqlite")
    look = float(cfg["activity_lookahead_hours"])
    _quote(store, "basketball_nba", NOW + timedelta(hours=look + 24), NOW)
    _quote(store, "baseball_mlb", NOW + timedelta(hours=6), NOW)
    active = active_sports(cfg, store, NOW)
    assert "basketball_nba" not in active
    assert "baseball_mlb" in active


def test_gating_is_not_a_trapdoor(cfg, tmp_path):
    """A gated sport stops producing quotes, so it must be re-probed or it can
    never prove itself active again."""
    store = Store(tmp_path / "t.sqlite")
    stale = NOW - timedelta(seconds=float(cfg["activity_reprobe_secs"]) + 60)
    _quote(store, "basketball_nba", NOW + timedelta(days=200), stale)
    assert "basketball_nba" in active_sports(cfg, store, NOW)


def test_never_probed_sport_is_active(cfg, tmp_path):
    """Cold start must fetch everything, or nothing is ever discovered."""
    store = Store(tmp_path / "t.sqlite")
    assert active_sports(cfg, store, NOW) == list(cfg["sports"])


def test_result_is_a_subset_of_configured_sports_in_order(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    for sport in cfg["sports"]:
        _quote(store, sport, NOW + timedelta(hours=6), NOW)
    assert active_sports(cfg, store, NOW) == list(cfg["sports"])


def test_dead_break_keys_are_gone(cfg):
    """scan_breaks/break_windows were schema-validated and read by nothing."""
    assert "scan_breaks" not in cfg
    assert "break_windows" not in cfg
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `pytest tests/test_activity.py -v`
Expected: all FAILED — `ModuleNotFoundError: No module named 'arb.activity'`.

- [ ] **Step 6: Implement the module**

Create `arb/activity.py`:

```python
"""Which sports are worth fetching this cycle (SPEC §13.1, config
activity_lookahead_hours). Derived entirely from stored quotes, so it costs no
request. Ungated, the July 2026 loop pulled 439,152 out-of-season NBA quotes,
100% unmatched, 0 flags — and `SELECT COUNT(*) FROM opportunities WHERE
event_key=''` is 0 of 57,690: an unmatched quote has never produced a flag."""
from __future__ import annotations

from datetime import datetime, timedelta

from arb.logutil import get_logger

log = get_logger("arb.activity", "logs/scan.log")


def active_sports(cfg, store, now: datetime) -> list[str]:
    """A sport is fetched if it has a known fixture inside the lookahead OR its
    newest stored quote is stale. The second clause is what stops the gate being
    a trapdoor: a gated sport produces no quotes, so it could never otherwise
    prove itself active again. Never-probed sports are always active."""
    look = float(cfg["activity_lookahead_hours"])
    reprobe = float(cfg["activity_reprobe_secs"])
    horizon = (now + timedelta(hours=look)).isoformat()
    out = []
    for sport in cfg["sports"]:
        row = store.query(
            """SELECT
                 (SELECT COUNT(*) FROM quotes
                  WHERE sport=? AND start_utc > ? AND start_utc <= ?) fixtures,
                 (SELECT MAX(fetched_at) FROM quotes WHERE sport=?) seen""",
            (sport, now.isoformat(), horizon, sport))[0]
        if row["fixtures"]:
            out.append(sport)
            continue
        seen = row["seen"]
        if seen is None or \
                (now - datetime.fromisoformat(seen)).total_seconds() > reprobe:
            out.append(sport)  # never probed, or stale: probe to re-discover
        else:
            log.info("sport %s gated this cycle: no fixture within %sh", sport, look)
    return out
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `pytest tests/test_activity.py -v`
Expected: 6 PASSED.

- [ ] **Step 8: Wire it into the fan-out**

In `arb/watch.py`, change the `fetch_all_venues` signature and add the filter. Replace lines 21-24:

```python
def fetch_all_venues(cfg) -> tuple[dict, dict]:
    """-> (quotes_by_venue, status_by_venue). A venue that fails marks the poll
    partial for that venue (§10.2) — it must never mass-close lifetimes."""
    quotes, status = {}, {}
```

with:

```python
def fetch_all_venues(cfg, store=None) -> tuple[dict, dict]:
    """-> (quotes_by_venue, status_by_venue). A venue that fails marks the poll
    partial for that venue (§10.2) — it must never mass-close lifetimes.

    With a store, the sports list is gated by arb.activity first; without one the
    full configured list is fetched (unchanged behaviour, used by tests)."""
    quotes, status = {}, {}
    if store is not None:
        from datetime import datetime, timezone
        from arb.activity import active_sports
        active = active_sports(cfg, store, datetime.now(timezone.utc))
        if active != list(cfg["sports"]):
            log.info("activity gate: fetching %s of %s sports (%s)",
                     len(active), len(cfg["sports"]), ", ".join(active))
        cfg = {**cfg, "sports": active}
```

- [ ] **Step 9: Pass the store from the watch command**

In `arb/commands.py`, in `cmd_watch` (line 13), find the `fetch_all_venues(cfg)` call and change it to `fetch_all_venues(cfg, store)`.

- [ ] **Step 10: Verify the gate against the real DB without fetching**

Run:

```bash
python -c "
from datetime import datetime, timezone
from pathlib import Path
from arb.config import load
from arb.store import Store
from arb.activity import active_sports
cfg = load(Path('config.yaml'))
print(active_sports(cfg, Store(Path('data/arb.sqlite')), datetime.now(timezone.utc)))
"
```

Expected: a list shorter than `cfg["sports"]`. In late July, `basketball_nba` should be absent (out of season, no fixture within 48h). **This makes no network calls.** If the command takes more than ~2 seconds, the Step 3 index did not apply — run `arb status` once to trigger migrations, then retry.

- [ ] **Step 11: Run the full suite**

Run: `pytest -q`
Expected: all pass. `tests/test_venues.py` and `tests/test_fetch_latency.py` call `fetch_all_venues(cfg)` with no store and are unaffected by design.

- [ ] **Step 12: Commit**

```bash
git add arb/activity.py arb/watch.py arb/commands.py arb/schema.py arb/migrations.py arb/config.py config.yaml tests/test_activity.py && git commit -m "$(cat <<'EOF'
Implement activity gating; delete the two dead break keys

activity_lookahead_hours was schema-validated and read by no code path. Every
adapter iterated the full sports list unconditionally, so July pulled 439,152
out-of-season NBA quotes: 100% unmatched, 0 flags. Across the whole ledger,
COUNT(*) FROM opportunities WHERE event_key='' is 0 of 57,690 - an unmatched
quote has never produced a flag.

The gate is self-healing: a sport is fetched if it has a fixture inside the
lookahead OR its newest quote is stale, so gating cannot become a trapdoor.
Derived from stored quotes, so it costs no request. Indexed by MIGRATION_7;
without it the per-cycle query is a full scan of 13.7M rows.

scan_breaks/break_windows are deleted rather than implemented: SPEC promises a
halftime re-scan for NBA and soccer, and both have produced zero flags in 21
days.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Rotate serial iteration so deadline truncation samples uniformly

Every adapter iterates a fixed order every cycle, and the shared `venue_deadline_secs` (30s) truncates the same tail every time. SX hits the deadline on **3.15% of watch polls** (PM 0.92%, kalshi 0.18%), so SX's late leagues are not sampled less — they are sampled *never*. Rotating the start index converts a permanent blind spot into uniform sampling at no request cost.

Applied only where truncation actually bites: the SX league loop and the PM per-sport fan-out. Kalshi truncates 0.18% of the time and is left alone.

**Files:**
- Modify: `arb/venues/base.py` (add `rotate`)
- Modify: `arb/venues/sx.py` (league loop), `arb/venues/polymarket.py:137-139` (`fetch_markets`)
- Test: `tests/test_venues.py`

**Interfaces:**
- Produces: `arb.venues.base.rotate(seq: list, period_secs: float, now: float | None = None) -> list` — returns `seq` rotated left by `int(now // period_secs) % len(seq)`. `now` defaults to `time.time()`; tests inject it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_venues.py`:

```python
def test_rotate_advances_once_per_period():
    """The venue deadline truncates the same tail every cycle: sx hits it on
    3.15% of watch polls, so its late leagues are sampled never, not less."""
    from arb.venues.base import rotate
    seq = ["a", "b", "c", "d"]
    assert rotate(seq, 60.0, now=0.0) == ["a", "b", "c", "d"]
    assert rotate(seq, 60.0, now=59.0) == ["a", "b", "c", "d"]
    assert rotate(seq, 60.0, now=60.0) == ["b", "c", "d", "a"]
    assert rotate(seq, 60.0, now=180.0) == ["d", "a", "b", "c"]
    assert rotate(seq, 60.0, now=240.0) == ["a", "b", "c", "d"]


def test_rotate_is_total_and_non_destructive():
    from arb.venues.base import rotate
    seq = ["a", "b", "c"]
    assert rotate([], 60.0, now=99.0) == []
    assert sorted(rotate(seq, 60.0, now=61.0)) == sorted(seq)
    assert seq == ["a", "b", "c"]  # input untouched
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_venues.py -k "rotate" -v`
Expected: 2 FAILED — `ImportError: cannot import name 'rotate' from 'arb.venues.base'`.

- [ ] **Step 3: Implement `rotate`**

In `arb/venues/base.py`, after the `Deadline` class (which ends with `remaining` at line 63), add:

```python
def rotate(seq: list, period_secs: float, now: float | None = None) -> list:
    """Rotate a serial iteration order once per period. The venue deadline
    truncates whatever is last, so a fixed order does not under-sample the tail —
    it never samples it (sx hits the deadline on 3.15% of watch polls). Rotating
    turns a permanent blind spot into uniform sampling, at no request cost."""
    if not seq:
        return []
    i = int((time.time() if now is None else now) // period_secs) % len(seq)
    return seq[i:] + seq[:i]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_venues.py -k "rotate" -v`
Expected: 2 PASSED.

- [ ] **Step 5: Apply it to the SX league loop**

In `arb/venues/sx.py`, `_fetch_sport` fetches `leagues` at line 91 and iterates it at line 93. Change line 93 from:

```python
    for lg in leagues:
```

to:

```python
    for lg in rotate(leagues, float(cfg["watch_interval_secs"])):
```

and extend the import on line 17 to:

```python
from arb.venues.base import Deadline, PartialFetch, get_json, is_live, rotate
```

- [ ] **Step 6: Apply it to the PM sport fan-out**

In `arb/venues/polymarket.py`, change line 138:

```python
    sports = list(cfg["sports"])
```

to:

```python
    # rotate: the shared deadline truncates whatever is last, every cycle
    sports = rotate(list(cfg["sports"]), float(cfg["watch_interval_secs"]))
```

Add `rotate` to the existing `from arb.venues.base import ...` line at the top of the file.

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: all pass. Any test asserting a specific fetch *order* would now be time-dependent and flaky — if one fails, pin it by passing an explicit `now` rather than by removing the rotation.

- [ ] **Step 8: Commit**

```bash
git add arb/venues/base.py arb/venues/sx.py arb/venues/polymarket.py tests/test_venues.py && git commit -m "$(cat <<'EOF'
Venues: rotate serial iteration under the shared deadline

Every adapter iterates a fixed order every cycle and venue_deadline_secs cuts
the same tail each time. sx hits the deadline on 3.15% of watch polls (pm 0.92%,
kalshi 0.18%), so its late leagues are sampled never, not less.

Rotating once per watch interval turns a permanent blind spot into uniform
sampling at zero request cost. Applied to the sx league loop and the pm sport
fan-out; kalshi truncates 0.18% of the time and is left alone.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Stop fetching Polymarket books that cannot produce a flag

`arb/venues/polymarket.py:122-128` collects every in-scope token and pulls a book for all of them: **3,469 tokens per poll → 35 `POST /books` calls** at `BOOKS_CHUNK = 100`. Of those tokens, 2,880 are either soccer or zero-volume. **120,411 of 208,383 PM quotes (57.8%) carry `volume_24h = 0`**, and only 14,932 trip the `zombie_spread_min` tag — so ~105k zero-volume quotes per 60 polls are fetched and pass every guard silently.

The Gamma response already parsed at `arb/venues/polymarket.py:41` carries `volume24hr` on every market object. Using it as a *fetch filter* is not pricing, so the stale-Gamma-mid trap is untouched.

**Rule 7 note:** rule 7 requires persisting every quote *observed*. Choosing not to observe a market is permitted; discarding an observed quote is not. Setting the threshold to `0` restores today's behaviour exactly, so this is reversible with one config key.

**Files:**
- Modify: `arb/venues/polymarket.py` (`_fetch_sport`)
- Modify: `config.yaml` (`polymarket.books_min_volume_24h`), `arb/config.py` (schema)
- Test: `tests/test_venues.py`

**Interfaces:**
- Produces: `arb.venues.polymarket._books_wanted(cfg, events: list[dict]) -> list[str]` — the token ids to request books for.
- Consumes: `cfg["polymarket"]["books_min_volume_24h"]`.

- [ ] **Step 1: Add the config key**

In `config.yaml`, add one line inside the existing `polymarket:` block (line 118):

```yaml
               books_min_volume_24h: 1,   # skip CLOB books for markets Gamma already
                                          #   reports as untraded: 120,411 of 208,383 PM
                                          #   quotes (57.8%) carry volume_24h=0, and only
                                          #   14,932 trip zombie_spread_min. Cuts ~35 book
                                          #   POSTs/poll to ~6. Set 0 to restore the old
                                          #   fetch-everything behaviour exactly.
```

- [ ] **Step 2: Declare it in the schema validator**

In `arb/config.py`, add `"books_min_volume_24h": NUM` to the existing `"polymarket"` schema entry.

- [ ] **Step 3: Write the failing test**

Append to `tests/test_venues.py`:

```python
def test_books_wanted_skips_untraded_markets(cfg):
    """Gamma already carries volume24hr on every market object; using it as a
    FETCH filter is not pricing, so the stale-Gamma-mid trap is untouched."""
    from arb.venues.polymarket import _books_wanted, IN_SCOPE_TYPES
    mtype = sorted(IN_SCOPE_TYPES)[0]
    events = [{"markets": [
        {"sportsMarketType": mtype, "clobTokenIds": '["hot1","hot2"]',
         "volume24hr": 5000},
        {"sportsMarketType": mtype, "clobTokenIds": '["cold1","cold2"]',
         "volume24hr": 0},
    ]}]
    assert _books_wanted(cfg, events) == ["hot1", "hot2"]


def test_books_wanted_threshold_zero_restores_old_behaviour(cfg):
    from arb.venues.polymarket import _books_wanted, IN_SCOPE_TYPES
    mtype = sorted(IN_SCOPE_TYPES)[0]
    events = [{"markets": [
        {"sportsMarketType": mtype, "clobTokenIds": '["a"]', "volume24hr": 0}]}]
    c = {**cfg, "polymarket": {**cfg["polymarket"], "books_min_volume_24h": 0}}
    assert _books_wanted(c, events) == ["a"]


def test_books_wanted_keeps_markets_with_no_volume_field(cfg):
    """A missing field is unknown, not zero — never gate on absent data."""
    from arb.venues.polymarket import _books_wanted, IN_SCOPE_TYPES
    mtype = sorted(IN_SCOPE_TYPES)[0]
    events = [{"markets": [{"sportsMarketType": mtype, "clobTokenIds": '["a"]'}]}]
    assert _books_wanted(cfg, events) == ["a"]
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `pytest tests/test_venues.py -k "books_wanted" -v`
Expected: 3 FAILED — `ImportError: cannot import name '_books_wanted'`.

- [ ] **Step 5: Extract and filter**

In `arb/venues/polymarket.py`, replace the `wanted` construction inside `_fetch_sport` (lines 122-128):

```python
    wanted = []
    for e in events:
        for m in e.get("markets", []):
            if m.get("sportsMarketType") in IN_SCOPE_TYPES and m.get("clobTokenIds"):
                wanted.extend(json.loads(m["clobTokenIds"]))
    books = fetch_books(cfg, wanted, deadline=deadline)
```

with:

```python
    books = fetch_books(cfg, _books_wanted(cfg, events), deadline=deadline)
```

and add, immediately above `_fetch_sport`:

```python
def _books_wanted(cfg, events: list[dict]) -> list[str]:
    """Token ids worth a CLOB book call. Gamma already reports volume24hr in the
    response we parse, and 57.8% of stored PM quotes carry volume_24h=0 — books
    for those cost 29 of the 35 POSTs per poll and have never produced a flag.
    A MISSING volume field is unknown, not zero: never gate on absent data.
    Threshold 0 restores fetch-everything (rule 7 governs what is persisted once
    observed, not what must be observed)."""
    floor = float(cfg["polymarket"]["books_min_volume_24h"])
    wanted = []
    for e in events:
        for m in e.get("markets", []):
            if m.get("sportsMarketType") not in IN_SCOPE_TYPES or \
                    not m.get("clobTokenIds"):
                continue
            vol = m.get("volume24hr")
            if vol is not None and float(vol) < floor:
                continue
            wanted.extend(json.loads(m["clobTokenIds"]))
    return wanted
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_venues.py -k "books_wanted" -v`
Expected: 3 PASSED.

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: all pass. If a PM fixture test asserts a quote count that now drops, check *why* — a fixture market with `volume24hr: 0` being skipped is correct; a market with no volume field being skipped is a bug in Step 5.

- [ ] **Step 8: Measure the saving on the next real poll**

After the next supervised watch poll, run:

```bash
sqlite3 data/arb.sqlite "SELECT AVG(n_quotes), AVG(latency_ms) FROM poll_venues v JOIN polls p ON p.id=v.poll_id WHERE v.venue='polymarket' AND p.kind='watch' AND p.started_at > datetime('now','-15 minutes')"
```

Expected: PM `latency_ms` well below its 6,519 ms watch-lane median. Record the number in the commit message.

- [ ] **Step 9: Commit**

```bash
git add arb/venues/polymarket.py arb/config.py config.yaml tests/test_venues.py && git commit -m "$(cat <<'EOF'
Polymarket: skip CLOB books for markets Gamma reports as untraded

_fetch_sport pulled a book for all 3,469 in-scope tokens per poll - 35 POST
/books calls - though 120,411 of 208,383 stored PM quotes (57.8%) carry
volume_24h=0 and only 14,932 trip zombie_spread_min.

volume24hr is already in the Gamma response we parse. Using it as a FETCH filter
is not pricing, so the stale-Gamma-mid trap is untouched. A missing field is
unknown, not zero. books_min_volume_24h: 0 restores the old behaviour exactly.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: Move the SX order-chunk size to config and raise it

`arb/venues/sx.py:112` hardcodes `for i in range(0, len(markets), 20)` — a rule 4 violation, and conservative. The live lane sends the whole hash list unchunked (`arb/lanes/live.py:135-136`) and has served **30 hashes in one request 157 times with zero failures** (max observed 30). SX is the critical path in every cycle at a 9,556 ms watch-lane median, so a third fewer orders calls is the largest single cut available to it.

**Files:**
- Modify: `arb/venues/sx.py:112`
- Modify: `config.yaml` (`sx.orders_chunk`), `arb/config.py` (schema)
- Test: `tests/test_venues.py`

**Interfaces:**
- Consumes: `cfg["sx"]["orders_chunk"]`.

- [ ] **Step 1: Add the config key**

In `config.yaml`, add to the existing `sx:` block (line 121):

```yaml
             orders_chunk: 30,      # marketHashes per /orders call. Was hardcoded 20
                                    #   (rule 4 miss). The live lane has served 30 in one
                                    #   request 157 times with 0 failures; sx is the
                                    #   critical path at a 9,556ms watch-lane median, so
                                    #   this is its largest single cut. Verify before
                                    #   raising further (rule 3).
```

- [ ] **Step 2: Declare it in the schema validator**

In `arb/config.py`, add `"orders_chunk": NUM` to the existing `"sx"` schema entry.

- [ ] **Step 3: Write the failing test**

Append to `tests/test_venues.py`:

```python
def test_sx_orders_chunk_comes_from_config(cfg):
    """Rule 4: the chunk was hardcoded 20. The live lane proves 30 works."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "arb" / "venues" / "sx.py").read_text()
    assert not re.search(r"range\(0, len\(markets\), 20\)", src)
    assert 'cfg["sx"]["orders_chunk"]' in src
    assert int(cfg["sx"]["orders_chunk"]) == 30
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `pytest tests/test_venues.py::test_sx_orders_chunk_comes_from_config -v`
Expected: FAIL on the first assertion — the hardcoded literal is still present.

- [ ] **Step 5: Read the chunk from config**

In `arb/venues/sx.py`, replace line 112:

```python
        for i in range(0, len(markets), 20):
```

with:

```python
        chunk_n = int(cfg["sx"]["orders_chunk"])
        for i in range(0, len(markets), chunk_n):
```

and on the following line replace `chunk = markets[i:i + 20]` with `chunk = markets[i:i + chunk_n]`.

- [ ] **Step 6: Run the test to verify it passes**

Run: `pytest tests/test_venues.py::test_sx_orders_chunk_comes_from_config -v`
Expected: PASSED.

- [ ] **Step 7: Run the full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 8: Verify against live SX once, by hand**

Run: `arb watch --once`
Expected: exits 0 with a non-zero sx quote count in the poll summary line. Then confirm SX did not start erroring:

```bash
sqlite3 data/arb.sqlite "SELECT ok, n_quotes, latency_ms FROM poll_venues WHERE venue='sx' ORDER BY poll_id DESC LIMIT 3"
```

Expected: `ok = 1` with a non-zero `n_quotes`. If SX returns 4xx at chunk 30, **revert to 20 in config** — rule 3 says stop and report rather than guess; the CLI flag is a config value precisely so this is a one-line rollback.

- [ ] **Step 9: Commit**

```bash
git add arb/venues/sx.py arb/config.py config.yaml tests/test_venues.py && git commit -m "$(cat <<'EOF'
SX: orders chunk to config, 20 -> 30

The chunk was hardcoded (rule 4 miss) and conservative. The live lane sends the
whole hash list unchunked and has served 30 in one request 157 times with zero
failures. sx is the critical path in every cycle at a 9,556ms watch-lane median,
so a third fewer orders calls is its largest single cut.

Verified live against the venue before commit; config value so a rollback to 20
is one line.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Cache the MLB schedule per NY date

`arb/cycle.py:53-63` (`fetch_mlb_schedule`) runs one uncached GET to `statsapi.mlb.com` **every cycle** for a schedule that changes once a day. It is called at `arb/cycle.py:108`, *after* the threaded fan-out, so it adds serially to the cycle with `venue_fetch_timeout_secs` (15s) x `venue_max_retries` (2) of tail risk on the critical path.

**Files:**
- Modify: `arb/cycle.py:53-63`
- Test: `tests/test_live_cycle.py`

**Interfaces:**
- Unchanged: `fetch_mlb_schedule(cfg)` keeps its signature and return type. It gains a module-level cache keyed on the NY date.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live_cycle.py`:

```python
def test_mlb_schedule_is_fetched_once_per_ny_date(cfg, monkeypatch):
    """One uncached GET per cycle, serially after the fan-out, for a schedule
    that changes once a day — with 15s x 2 retries of tail risk."""
    import arb.cycle as C
    from arb.venues import mlbstats
    calls = []

    def counting(cfg_, date_str):
        calls.append(date_str)
        return [{"gamePk": 1}]

    monkeypatch.setattr(mlbstats, "fetch_schedule", counting)
    monkeypatch.setattr(C, "_SCHEDULE_CACHE", {}, raising=False)

    first = C.fetch_mlb_schedule(cfg)
    second = C.fetch_mlb_schedule(cfg)

    assert first == second == [{"gamePk": 1}]
    assert len(calls) == 1


def test_mlb_schedule_failure_is_not_cached(cfg, monkeypatch):
    """A transient outage must not blind the matcher for the rest of the day."""
    import arb.cycle as C
    from arb.venues import mlbstats
    calls = []

    def flaky(cfg_, date_str):
        calls.append(date_str)
        if len(calls) == 1:
            raise RuntimeError("statsapi down")
        return [{"gamePk": 2}]

    monkeypatch.setattr(mlbstats, "fetch_schedule", flaky)
    monkeypatch.setattr(C, "_SCHEDULE_CACHE", {}, raising=False)

    assert C.fetch_mlb_schedule(cfg) is None
    assert C.fetch_mlb_schedule(cfg) == [{"gamePk": 2}]
    assert len(calls) == 2
```

If `tests/test_live_cycle.py` has no `cfg` fixture, copy the one from `tests/test_ops.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_live_cycle.py -k "mlb_schedule" -v`
Expected: `test_mlb_schedule_is_fetched_once_per_ny_date` FAILED on `assert len(calls) == 1` (it is 2).

- [ ] **Step 3: Add the per-date cache**

In `arb/cycle.py`, replace `fetch_mlb_schedule` (lines 53-63) with:

```python
_SCHEDULE_CACHE: dict[str, list] = {}


def fetch_mlb_schedule(cfg):
    """Cached per NY date: the schedule changes once a day, but this ran every
    cycle — uncached, serially AFTER the threaded fan-out, carrying 15s x 2
    retries of tail risk on the critical path. Failures are NOT cached: a
    transient statsapi outage must not blind the matcher for the rest of the day."""
    if not cfg["mlbstats_enabled"]:
        return None
    from arb.venues.mlbstats import fetch_schedule
    today = datetime.now(NY).date().isoformat()
    if today in _SCHEDULE_CACHE:
        return _SCHEDULE_CACHE[today]
    try:
        games = fetch_schedule(cfg, today)
    except Exception as e:  # noqa: BLE001 — schedule is an upgrade, not a dependency
        log.warning("mlb schedule fetch failed: %s", e)
        return None
    _SCHEDULE_CACHE.clear()  # one date at a time; the day rolled over
    _SCHEDULE_CACHE[today] = games
    return games
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_live_cycle.py -k "mlb_schedule" -v`
Expected: 2 PASSED.

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add arb/cycle.py tests/test_live_cycle.py && git commit -m "$(cat <<'EOF'
Cycle: cache the MLB schedule per NY date

fetch_mlb_schedule ran one uncached GET to statsapi.mlb.com every cycle for a
schedule that changes once a day - and it runs serially AFTER the threaded
fan-out, so its 15s x 2 retry ladder sits on the critical path.

Failures are deliberately not cached: a transient outage must not blind the
matcher for the rest of the day.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

**Phase 3 gate.** Run `pytest -q`, then let the supervisor run for 30 minutes and compare against the pre-phase baseline:

```bash
sqlite3 data/arb.sqlite "SELECT venue, COUNT(*) polls, ROUND(AVG(n_quotes)) quotes, ROUND(AVG(latency_ms)) lat, SUM(ok=0) not_ok FROM poll_venues v JOIN polls p ON p.id=v.poll_id WHERE p.kind='watch' AND p.started_at > datetime('now','-30 minutes') GROUP BY venue"
```

Expected: quotes per poll down substantially (the gate removes out-of-season sports, the PM filter removes untraded books), latency down on PM and SX, `not_ok` no worse than before. **Tripwire:** if `not_ok` rises or flags per poll fall, revert Task 11 first (`books_min_volume_24h: 0`) — it is the only change that can remove a market from view.

---

# Phase 4 — Documentation and ops hygiene

### Task 14: Delete the dead `hot` config block

`config.yaml:200-201` defines a five-key `hot:` block, `arb/config.py:112-113` validates it, and `arb/cli.py:66` registers `hot` as a subcommand — but **there is no handler and no module.** `arb/cli.py:125-128` does `getattr(commands, "cmd_hot", None)`; `grep -n "^def cmd_" arb/commands.py` has no `cmd_hot`; `arb/lanes/` contains only `live.py`, `political.py`, `verify.py`. Running `arb hot` prints `hot: not implemented yet` and returns 1. Nothing anywhere reads `hot.poll_secs`.

This dead config has already caused a planning document to assert capability that does not exist (`SPEEDPLAN.md:107`: *"The `hot` lane already exists in config … This is largely wiring what is already there."*). Its one genuinely useful knob, `dead_checks`, moved to `tracker` in Task 1.

**Files:**
- Modify: `config.yaml` (delete the `hot:` block), `arb/config.py:112-113` (delete the schema entry), `arb/cli.py:66` (drop `hot` from the subcommand tuple)
- Test: `tests/test_hygiene.py`

**Interfaces:** None. Removal only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hygiene.py`:

```python
def test_no_config_key_is_validated_but_unread():
    """A schema-validated knob that no code reads is worse than no knob: the hot
    block made SPEEDPLAN.md assert a lane that does not exist."""
    import re
    from pathlib import Path
    from arb.config import load
    root = Path(__file__).resolve().parent.parent
    cfg = load(root / "config.yaml")
    assert "hot" not in cfg
    src = "\n".join(p.read_text() for p in _arb_sources())
    for dead in ("hot.poll_secs", '"hot"', "scan_breaks", "break_windows"):
        assert not re.search(re.escape(dead), src), dead
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_hygiene.py::test_no_config_key_is_validated_but_unread -v`
Expected: FAILED on `assert "hot" not in cfg`.

- [ ] **Step 3: Delete the config block**

In `config.yaml`, delete the two lines beginning `hot:    { poll_secs: 2, ...` and their continuation (line 200-201, ending `max_watchers: 3 }   # 900s: ...`).

- [ ] **Step 4: Delete the schema entry**

In `arb/config.py`, delete lines 112-113:

```python
    "hot": {"poll_secs": NUM, "check_interval_secs": NUM, "watch_duration_secs": NUM,
            "dead_checks": NUM, "max_watchers": NUM},
```

- [ ] **Step 5: Drop the phantom subcommand**

In `arb/cli.py`, change line 66 from:

```python
    for lane in ("negrisk-scan", "verify", "hot", "btc", "political", "mapped"):
```

to:

```python
    for lane in ("negrisk-scan", "verify", "btc", "political", "mapped"):
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `pytest tests/test_hygiene.py::test_no_config_key_is_validated_but_unread -v`
Expected: PASSED.

- [ ] **Step 7: Run the full suite and confirm the CLI**

Run: `pytest -q && arb --help`
Expected: all tests pass; `hot` is absent from the subcommand list, `verify` is present.

- [ ] **Step 8: Commit**

```bash
git add config.yaml arb/config.py arb/cli.py tests/test_hygiene.py && git commit -m "$(cat <<'EOF'
Delete the dead hot config block and its phantom subcommand

hot: was five schema-validated keys, a registered CLI subcommand with no
handler, and a SPEC entry for arb/lanes/hot.py that was never written. Nothing
read hot.poll_secs. It made SPEEDPLAN.md assert "the hot lane already exists in
config... largely wiring what is already there" - it did not exist.

Its one useful knob, dead_checks, moved to tracker. A test now blocks any
schema-validated key that no code reads.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: Correct SPEEDPLAN.md and rotate the supervisor's stdout

`SPEEDPLAN.md` §0 reports "kalshi 573 ms, PM 249 ms, SX 759 ms" and concludes "~15–20 HTTP requests, ~1–2 s wall clock" and "we are using ~2% of the wall-clock budget". Those medians are computed across **all** poll kinds and are dominated by the 35,094 fast `live` polls. Watch-lane reality: kalshi p50 775 ms, **PM p50 6,519 ms** (26× the stated figure), **SX p50 9,556 ms** (12×), with ~90–105 requests landing inside the first ~10s ≈ 8 req/s instantaneous against the ~2 req/s ceiling the same document sets in §2. The document's central conclusion does not survive its own methodology, and Tier 0/1 decisions are being taken on it.

Separately, `logs/supervisor-stdout.log` is **1.49 GB**, last written 2026-07-17, unrotated. `log_retention_days: 90` covers the `logutil` handlers, not the supervisor's stdout redirect.

Finally, the 18% dead time documented in Task 6 has **no known cause**. This task records what is known and leaves diagnosis to a real debugging session — inventing a fix for an undiagnosed outage would be worse than naming it.

**Files:**
- Modify: `SPEEDPLAN.md`
- Modify: whichever script performs the supervisor stdout redirect (find it: `grep -rn "supervisor-stdout" --include=* .`)

**Interfaces:** None.

- [ ] **Step 1: Correct the SPEEDPLAN measurements**

In `SPEEDPLAN.md`, replace the latency row and the work-per-cycle row of the §0 table with watch-lane-only figures, and append this note directly under that table:

```markdown
> **Amended 2026-07-29.** The latency figures originally in this table were
> computed across all poll kinds and were dominated by 35,094 fast `live` polls.
> Watch-lane only: kalshi p50 775 ms / p90 1,539; **polymarket p50 6,519 ms** /
> p90 9,691 / max 281,378, `ok=0` on **26.7%** of watch polls; **sx p50 9,556 ms**
> / p90 21,042. Requests per cycle are **~90–105**, not 15–20, and they land
> inside the first ~10 s — roughly **8 req/s instantaneous** against the ~2 req/s
> sustained ceiling §2 sets. §0's conclusion ("the pipeline is not slow… ~1–2 s
> wall clock… we are using ~2% of the wall-clock budget") does not survive this
> correction and should not be used to justify Tier 0/1 sequencing.
```

- [ ] **Step 2: Correct the hot-lane claim**

In `SPEEDPLAN.md` §Tier 1, replace the sentence *"The `hot` lane already exists in config … This is largely wiring what is already there."* with:

```markdown
**The `hot` lane does not exist.** The config block did (five validated keys, a
CLI subcommand with no handler, a SPEC entry for `arb/lanes/hot.py` that was
never written); it was deleted 2026-07-29. Its `dead_checks` knob moved to
`tracker`. What *does* exist and was never supervised is `arb/lanes/verify.py`,
enabled 2026-07-29. Tier 1 is a build, not a wiring job — though
`arb/lanes/live.py:95-124` already implements the discover-slow / re-quote-fast
pattern it needs.
```

- [ ] **Step 3: Record the death attribution**

In `SPEEDPLAN.md` §1, append under the confirm-rate bullet:

```markdown
- **Death attribution (measured 2026-07-28, 127 closed bets):** 68% of deaths are
  the market vanishing from the venue payload (99% never return; 76% were >6 h
  from event start — the illiquid early-line class), and only 24% are the edge
  genuinely closing. Confirm rate *rises* with distance from start (77.5% at
  >48 h vs 50% at 1–6 h) and with edge size — a stale-book signature, not a
  fleeting mispricing. **Cadence addresses the 24%, not the 68%.** A separate
  19.5% of closures were bookkeeping deaths under `absent-one-poll`, fixed
  2026-07-29 by `tracker.dead_checks`.
```

- [ ] **Step 4: Record the uptime problem as open**

In `SPEEDPLAN.md` §5, append:

```markdown
**Open, undiagnosed (2026-07-29): 18% of wall-clock is dead.** 56 gaps over
5 minutes since 07-25; 19.9 dead hours out of 113. It is inverted against yield —
hour 22 is 37.7% dead, hour 21 27.6%, hour 19 24.7%, while hours 14/15/06 are
0.0% dead and produce nothing; hours 19–22 hold 73% of all ARB flags. The
07-26 evening cluster (112 + 84 + 68 + 61 min, 16:28→22:34) sat on the single
highest-yield window of the day. 07-28 achieved 1,433 of 1,440 polls, so the loop
can hit its target. **Cause unknown.** Per-lane silence alarms landed 2026-07-29
(`heartbeat.stale_lanes`) so the next occurrence is at least visible. Diagnose
before any cadence increase: 3× the cadence on an 82%-uptime loop is still 82%.
```

- [ ] **Step 5: Find and fix the stdout redirect**

Run: `grep -rn "supervisor-stdout" --include="*.sh" --include="*.py" --include="*.plist" . scripts/`
Expected: one match, in the launcher that starts the supervisor. Add log rotation to it — prefer the platform's own facility over new code. If it is a shell launcher, the smallest correct change is to truncate on start and cap the file:

```bash
# supervisor stdout is a raw redirect, outside log_retention_days (logutil only
# rotates its own handlers). 1.49 GB accumulated by 2026-07-17.
[ -f logs/supervisor-stdout.log ] && mv -f logs/supervisor-stdout.log logs/supervisor-stdout.log.1
```

placed immediately before the redirect. If no launcher exists and the redirect is done by hand, note that in `SPEEDPLAN.md` instead of inventing one.

- [ ] **Step 6: Reclaim the space**

Run: `ls -lh logs/supervisor-stdout.log && du -sh logs/`
Expected: shows the 1.49 GB file. Then, **after confirming the supervisor is not running** (`arb status`):

```bash
mv logs/supervisor-stdout.log logs/supervisor-stdout.log.1 && gzip logs/supervisor-stdout.log.1
```

Do not delete it — it is 21 days of forensics for the undiagnosed outages in Step 4.

- [ ] **Step 7: Verify the docs are consistent**

Run: `grep -n "hot lane\|hot\.poll_secs\|249 ms\|15–20" SPEEDPLAN.md`
Expected: no match for the corrected claims, or only matches inside the amendment blocks that quote them as superseded.

- [ ] **Step 8: Run the full suite**

Run: `pytest -q`
Expected: all pass. (Docs-only changes, but the gate is unconditional.)

- [ ] **Step 9: Commit**

```bash
git add SPEEDPLAN.md scripts/ && git commit -m "$(cat <<'EOF'
SPEEDPLAN: correct the measurements its conclusions rest on

§0's latencies were all-poll-kind medians dominated by 35,094 fast live polls.
Watch-lane only: PM p50 6,519ms (26x the stated 249ms) with ok=0 on 26.7% of
polls, SX p50 9,556ms (12x), ~90-105 requests per cycle landing in the first
~10s = ~8 req/s against the ~2 req/s ceiling the same document sets. "The
pipeline is not slow... ~2% of the wall-clock budget" does not survive.

Also corrects the hot-lane claim (the lane never existed; verify did and was
never supervised), records the measured death attribution (68% vanished markets
vs 24% closed edges - cadence addresses the smaller half), and records the 18%
dead-time problem as OPEN and undiagnosed rather than inventing a fix.

Rotates logs/supervisor-stdout.log: 1.49 GB, unrotated, outside
log_retention_days. Archived, not deleted - it is forensics for those outages.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Step 1: Full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 2: Ledger invariants**

Run: `arb check-invariants`
Expected: silent (it prints only when something is wrong).

- [ ] **Step 3: Report unchanged**

Run: `arb report --daily 2026-07-28 | diff /tmp/report-baseline.txt -`
Expected: no output. Every change in this plan is forward-looking; no historical row was rewritten (rule 9).

- [ ] **Step 4: Clean tree, committed HEAD**

Run: `git status --short`
Expected: empty. Rule 13 — loops run only from a committed HEAD after a green `pytest`.

- [ ] **Step 5: One supervised hour, then compare**

Run the supervisor for at least one hour, then:

```bash
sqlite3 data/arb.sqlite "
SELECT p.kind, COUNT(DISTINCT p.id) polls,
       ROUND(AVG(v.n_quotes)) quotes, ROUND(AVG(v.latency_ms)) lat,
       SUM(v.ok=0) not_ok
FROM polls p JOIN poll_venues v ON v.poll_id = p.id
WHERE p.started_at > datetime('now','-1 hour') GROUP BY p.kind;
SELECT json_extract(payload_json,'\$.reason') reason, COUNT(*)
FROM bet_events WHERE kind='lifetime_closed'
  AND created_at > datetime('now','-1 hour') GROUP BY reason;"
```

Expected: quotes per poll materially down; PM and SX latency down; `absent-n-polls` present and `absent-one-poll` absent; no `venue-gap-timeout` unless a venue really is down.

**Tripwires — revert if any fires:** 429 rate above 1 per 1,000 requests; venue `ok`-rate below its pre-plan level; `flat_bets.leg_skew_secs` p90 above 3 s; flags per poll falling.

## Deliberately not in this plan

Named so nobody assumes they were missed:

- **Any cadence number change.** The mechanism ships (Task 7); the numbers are a pre-registered policy decision, and `SPEEDPLAN.md` §5 requires each step ship with the measurement that proves or kills it. Decide after Phase 1's confirm-rate data exists.
- **Dropping soccer from `sports:`.** Soccer is 58.8% of the PM payload with zero aliases in `data/aliases.json`, so `arb/matching.py:52` drops every soccer quote before grouping — but a concurrent session is actively adding alias tables. Activity gating (Task 9) reduces the cost without pre-empting that work.
- **Conditional fetch on `updated_time` / book `hash`.** Both fields are present and unparsed (`tests/fixtures/kalshi_markets_winner.json`, `tests/fixtures/pm_clob_books_batch.json`). It is a genuine cut to the request numerator, but it is a correctness-sensitive change to the pricing path and deserves its own plan.
- **Kalshi WebSocket, a daemon collector, multi-IP.** SPEEDPLAN Tiers 2–3. Each needs its endpoint pinned in `data/verified_facts.md` first (rule 3), and Tier 3 would corrupt `leg_skew_secs` — currently the cleanest measurement in the system at 0.69 s median — without a master clock.
- **Diagnosing the 18% dead time.** Cause unknown. Task 6 makes it visible and Task 15 records it; diagnosis belongs in a `superpowers:systematic-debugging` session with the archived stdout log, not in a plan that would be guessing.
