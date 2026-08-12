# KNOWN_ISSUES Defect Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the five code-level defects from `KNOWN_ISSUES.md` that block measurement or let untradeable positions reach the clean lane.

**Architecture:** All five changes are either *additive* (persist a field that was dropped) or *restrictive* (refuse something that should never have been eligible). **None unlocks trading** — the §18.2 settlement-risk arithmetic (`net = gross − (3/n)·LGM`, currently −6.82pts at n=41) still gates every cross-venue position after these changes, and rule 1 forbids order placement regardless. This is deliberate: the instrument gets more honest, not more permissive.

**Tech Stack:** Python 3.13, stdlib `sqlite3`, `Decimal` for all money (rule 5), pytest. No new dependencies.

## Global Constraints

- **Rule 1:** paper trading only; public unauthenticated GETs/POSTs only. No order placement.
- **Rule 2:** no Odds API credits in tests or loops. All tests run against fixtures.
- **Rule 4:** no magic numbers — every threshold lives in `config.yaml` and is added to `SCHEMA` in `arb/config.py` (the loader rejects unknown *and* missing keys).
- **Rule 5:** money math is `Decimal`, quantized `ROUND_HALF_EVEN` to 6 places via `arb.models.q6` at store/compare boundaries only.
- **Rule 7:** persist every quote; guards **demote to REVIEW, never drop**.
- **Rule 9:** `flat_bets` and `bet_events` are append-only. No `UPDATE`/`DELETE` — a CI grep-test in `tests/test_hygiene.py` enforces this.
- **Rule 12:** `classify()` exists in exactly one module (`arb/eligibility.py`); a grep-test proves it. `classify` takes a **flat dict** and must stay pure — callers pin DB facts into the dict (see `arb/tracker.py:287-292`).
- **Workflow (binding):** reproduce → failing test → minimal implementation → full `pytest -q` → commit with evidence.
- **Test command:** `.venv/bin/python -m pytest -q` (must stay green; baseline is **360 passed**).
- **Interpreter:** `.venv/bin/python` — the framework Python lacks `dotenv`.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `arb/tracker.py` | build candidate dicts; pin caller-supplied DB facts into `classify` | 1, 3 |
| `arb/arbitrage.py` | detection guards and opportunity status | 2 |
| `arb/reconcile.py` | per-leg venue verdicts; new: derive the certificate from them | 3 |
| `arb/eligibility.py` | the single `(eligible, reason, lane)` predicate | 4, 5 |
| `config.yaml` + `arb/config.py` | thresholds and their schema | 2, 5 |
| `tests/test_tracker.py` | leg payload contents | 1 |
| `tests/test_arbitrage.py` | detection guards | 2 |
| `tests/test_reconcile.py` | certificate derivation | 3 |
| `tests/test_eligibility.py` | matrix gate, SX exclusion | 4, 5 |

## Out of Scope (separate plans)

- **Issue #7 (L5 lag is a sampling artifact)** — needs a burst collector polling both venues at ≤4s. That is a new subsystem with its own pre-registration; it belongs in its own plan.
- **Issues #2, #4, #22–25** — findings and arithmetic, not defects. Nothing to implement.
- **Issue #30** (`db_backup.dest` volume) — operator action, not code.

---

### Task 1: Persist leg depth so capacity is measurable (issue #8)

`arb/tracker.py:27-33` builds each leg dict without `depth_usd`, so `flat_bets.legs_json` stores `None` for it. Consequence: 74% of positions in the 30-day backtest used *imputed* depth, and an early analysis wrongly concluded "all legs have zero depth" because `Decimal(str(l.get("depth_usd") or 0))` turns `None` into `0`.

**Files:**
- Modify: `arb/tracker.py:27-33` (the `legs` list comprehension in `candidate_from_opp`)
- Test: `tests/test_tracker.py`

**Interfaces:**
- Consumes: `arb.models.Quote.depth_usd` (`Decimal | None`)
- Produces: each leg dict in `legs_json` gains key `"depth_usd"` → `str` of a `Decimal`, or `None`. `arb/projection.py` and `arb/banksim.py` already read this key and will start seeing real values.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tracker.py`:

```python
def test_candidate_legs_carry_depth_usd(cfg):
    """flat_bets.legs_json dropped depth_usd, so capacity could not be measured
    from the ledger: 74% of backtest positions used an imputed median instead,
    and `depth_usd or 0` silently read the missing field as zero depth."""
    from decimal import Decimal

    from arb.tracker import candidate_from_opp
    o = _opp(cfg, depth_a="250", depth_b="80")
    cand = candidate_from_opp(cfg, o)
    depths = [leg["depth_usd"] for leg in cand["legs"]]
    assert depths == ["250", "80"], f"depth not persisted: {depths}"
    assert all(Decimal(d) > 0 for d in depths)
```

Add this helper near the top of `tests/test_tracker.py` if `_opp` does not already exist (check first — reuse the file's existing builder if there is one):

```python
def _opp(cfg, depth_a="100", depth_b="100"):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from arb.matching import MatchedGroup
    from arb.models import Event, Opportunity, Quote
    now = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    start = now + timedelta(hours=2)

    def q(venue, outcome, cost, depth):
        c = Decimal(cost)
        return Quote(event_key="e", venue=venue, book=venue, market_type="WINNER",
                     line=None, outcome_label=outcome, side="YES", raw_price=c,
                     cost=c, fee_cost=Decimal("0"), depth_usd=Decimal(depth),
                     ladder_json=None, live=False, fetched_at=now,
                     category="sports", sport="baseball_mlb",
                     bid=c - Decimal("0.02"), volume_24h=Decimal("100"),
                     home="h", away="a", start_utc=start, start_grade="verified")
    legs = [q("kalshi", "mets", "0.47", depth_a), q("polymarket", "royals", "0.49", depth_b)]
    ev = Event(event_key="e", category="sports", sport="baseball_mlb", home="h",
               away="a", start_utc=start, start_grade="verified")
    return Opportunity(kind="XSPREAD", category="sports", event=ev, legs=legs,
                       edge=Decimal("0.04"), stakes=[Decimal("0.5"), Decimal("0.5")],
                       status="OK", notes="")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tracker.py::test_candidate_legs_carry_depth_usd -v`
Expected: FAIL with `KeyError: 'depth_usd'`

- [ ] **Step 3: Write minimal implementation**

In `arb/tracker.py`, add one key to the `legs` comprehension in `candidate_from_opp` (currently lines 27-33). The comprehension becomes:

```python
    legs = [{"venue": q.venue, "book": q.book, "market_type": q.market_type,
             "line": q.line, "outcome": q.outcome_label, "side": q.side,
             "venue_side": q.venue_side or q.side,
             "cost": str(q.cost), "fee_cost": str(q.fee_cost),
             # depth at entry: without it the ledger cannot answer "how big
             # could this have been", and `depth_usd or 0` reads a missing
             # field as zero depth rather than as unknown
             "depth_usd": str(q.depth_usd) if q.depth_usd is not None else None,
             "mid0": str(q6((q.cost + q.bid) / 2)) if q.bid is not None else None,
             "venue_market_id": q.venue_market_id,
             "fetched_at": q.fetched_at.isoformat()} for q in o.legs]
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 361 passed (360 baseline + 1 new)

- [ ] **Step 5: Commit**

```bash
git add arb/tracker.py tests/test_tracker.py
git commit -m "Tracker: persist leg depth_usd so capacity is measurable from the ledger

legs_json dropped depth_usd, so 74% of positions in the 30-day backtest used
an imputed median depth, and \`depth_usd or 0\` read the missing field as zero
depth rather than unknown — which produced a spurious 'every leg has zero
depth' conclusion. arb/projection.py and arb/banksim.py already read this key.

KNOWN_ISSUES #8."
```

---

### Task 2: Refuse opportunities whose edge is inside the spread (issues #6, #28)

The central empirical finding of the session: the edge is smaller than the cost of crossing. The `nonsports:map:kxcpicore` pair was flagged **234 times** with a 1.52¢ edge sitting inside an **11¢** Kalshi spread. The existing `_zombie` guard cannot catch this — it demands `volume_24h == 0` **AND** `spread >= 0.90`, and the median zero-volume quote has a **2¢** spread, so it fires on **0.76%** of dead books.

A spread-vs-edge test is the principled form: if the sum of half-spreads you must cross exceeds the edge, there is no opportunity at any size.

**Files:**
- Modify: `arb/arbitrage.py` — add `_spread_exceeds_edge` and call it in `_locked_opp`
- Modify: `config.yaml` (add `max_spread_fraction_of_edge` under `execution`)
- Modify: `arb/config.py` (add the key to `SCHEMA["execution"]`)
- Test: `tests/test_arbitrage.py`

**Interfaces:**
- Consumes: `Quote.cost`, `Quote.bid` (`Decimal | None`), `cfg["execution"]["max_spread_fraction_of_edge"]` (`Decimal`)
- Produces: `_locked_opp` appends note `spread>edge` and sets `status="REVIEW"`. No new return values; callers unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_arbitrage.py`:

```python
def test_edge_inside_the_spread_demotes_to_review():
    """Measured 2026-07-29: one CPI pair was flagged 234 times with a 1.52c edge
    inside an 11c Kalshi spread. The zombie guard cannot see it — that needs
    volume==0 AND a >=90c spread, and the median zero-volume quote has a 2c
    spread, so it fires on 0.76% of dead books. If the half-spreads you must
    cross exceed the edge, there is no opportunity at any size."""
    wide = [q("kalshi", "mets", cost="0.47", bid="0.36"),      # 11c spread
            q("polymarket", "royals", cost="0.49", bid="0.47")]  # 2c spread
    opps, _ = detect(CFG, [group(wide)], now=NOW)
    locked = [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert locked, "the covering pair should still be flagged (rule 7)"
    assert locked[0].status == "REVIEW"
    assert "spread>edge" in locked[0].notes


def test_tight_spread_is_not_demoted_for_spread():
    tight = [q("kalshi", "mets", cost="0.47", bid="0.4695"),
             q("polymarket", "royals", cost="0.49", bid="0.4895")]
    opps, _ = detect(CFG, [group(tight)], now=NOW)
    locked = [o for o in opps if o.kind in ("ARB", "XSPREAD")]
    assert locked
    assert "spread>edge" not in locked[0].notes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_arbitrage.py -k spread -v`
Expected: `test_edge_inside_the_spread_demotes_to_review` FAILS on `assert locked[0].status == "REVIEW"` (it is currently `"OK"`); the tight-spread test passes trivially.

- [ ] **Step 3: Add the config threshold**

In `config.yaml`, inside the `execution:` block, immediately after the `max_actionable_skew_secs: 10` line, add:

```yaml
  max_spread_fraction_of_edge: 1.0   # sum of leg half-spreads must not exceed this
                                     #   multiple of the edge. At 1.0: if crossing
                                     #   costs more than the edge, it is not an
                                     #   opportunity at any size. Measured 2026-07-29:
                                     #   one CPI pair was flagged 234x with a 1.52c
                                     #   edge inside an 11c Kalshi spread.
```

In `arb/config.py`, add the key to `SCHEMA["execution"]` next to `"max_actionable_skew_secs": NUM,`:

```python
                  "max_spread_fraction_of_edge": NUM,
```

- [ ] **Step 4: Write minimal implementation**

In `arb/arbitrage.py`, add this helper immediately after `_zombie`:

```python
def _spread_exceeds_edge(cfg, legs, edge: Decimal) -> bool:
    """True when the half-spreads we must cross cost more than the edge.

    The existing zombie guard cannot express this: it requires volume_24h == 0
    AND a >= 0.90 spread, but the median zero-volume quote has a 2c spread, so
    it catches 0.76% of dead books. A leg with no bid has an unknown spread and
    is treated as unusable, not as free."""
    total = Decimal(0)
    for q in legs:
        if q.bid is None:
            return True
        total += (q.cost - q.bid) / 2
    return total > edge * cfg["execution"]["max_spread_fraction_of_edge"]
```

In `_locked_opp`, immediately after the `thin-depth` block (which currently reads
`if any(q.depth_usd is None or q.depth_usd < cfg["min_executable_usd"] for q in legs):`
… `notes.append("thin-depth")`), insert:

```python
    if _spread_exceeds_edge(cfg, legs, edge):
        status = "REVIEW"
        notes.append("spread>edge")
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. If any pre-existing test now fails because its fixture uses a wide default spread, fix the *fixture* to a realistic spread — do not weaken the guard.

- [ ] **Step 6: Measure the blast radius with replay**

Run: `.venv/bin/python -m arb replay --from 2026-07-27 --to 2026-07-29`
Expected: fingerprints `replay-only=0`, `stored-only=0` (the detected set is unchanged — this only moves status), and `ARB/OK` + `XSPREAD/OK` counts lower than the stored ones. Record the before/after counts for the commit message.

- [ ] **Step 7: Commit**

```bash
git add arb/arbitrage.py config.yaml arb/config.py tests/test_arbitrage.py
git commit -m "Detection: demote an opportunity whose edge is inside the spread

The session's central finding, made into a guard. One CPI pair was flagged 234
times with a 1.52c edge inside an 11c Kalshi spread. The zombie guard cannot
express this: it needs volume_24h==0 AND a >=90c spread, and the median
zero-volume quote has a 2c spread, so it fires on 0.76% of dead books.

If the half-spreads you must cross exceed the edge, there is no opportunity at
any size. A leg with no bid has an unknown spread and counts as unusable, not
as free. Demote, never drop (rule 7).

Verified with arb replay: detected set unchanged, status only.

KNOWN_ISSUES #6, #28."
```

---

### Task 3: Derive `certificate_n` from the ledger (issue #1)

`arb/eligibility.py:77` reads `o.get("certificate_n")` and returns `"no-certificate"` when it is 0. **Nothing in the codebase ever writes it**, so every `XSPREAD` is review-lane forever regardless of configuration. This task supplies the missing number: the count of mismatch-free reconciled pairs for a venue pair.

The arithmetic still gates: at the 41 pairs we have, `(3/41)·LGM = 7.32pts` against a 0.5pt median edge, so `net_spread_edge` stays negative and nothing promotes. That is correct and intended — this task makes the gate *computable*, not passable.

**Files:**
- Modify: `arb/reconcile.py` — add `certificate_n`
- Modify: `arb/tracker.py:287-292` — pin it into `classify`'s input dict
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `bet_events` rows with `kind='reconciled'` and payload `{"verdicts": [...]}`; `flat_bets.venue_pair`, `flat_bets.kind`
- Produces: `reconcile.certificate_n(store, venue_pair: str) -> int` — the number of `XSPREAD` positions for that pair whose **both** legs graded to `WON`/`LOST`/`VOID` and which were complementary (exactly one `WON`). `tracker.process_poll` pins the result as `certificate_n`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reconcile.py`:

```python
def test_certificate_n_counts_only_fully_graded_complementary_pairs(cfg, tmp_path):
    """eligibility.py:77 reads certificate_n and nothing ever wrote it, so every
    XSPREAD returned 'no-certificate' forever. Verdicts arrive one leg per pass
    (reconcile is multi-pass), so they must be UNIONED per bet; UNRESOLVABLE is
    missing data, never evidence of agreement."""
    import json

    from arb.reconcile import certificate_n
    from arb.store import Store
    store = Store(tmp_path / "t.sqlite")
    rows = [
        ("kalshi:polymarket", "XSPREAD", [["WON", None], [None, "LOST"]]),   # counts
        ("kalshi:polymarket", "XSPREAD", [["WON", "LOST"]]),                 # counts
        ("kalshi:polymarket", "XSPREAD", [["WON", None]]),                   # one leg only
        ("kalshi:polymarket", "XSPREAD", [["UNRESOLVABLE", "UNRESOLVABLE"]]),
        ("kalshi:polymarket", "XSPREAD", [["WON", "WON"]]),                  # a MISMATCH
        ("kalshi:sx", "XSPREAD", [["WON", "LOST"]]),                         # other pair
        ("kalshi:self", "ARB", [["WON", "LOST"]]),                           # not XSPREAD
    ]
    for pair, kind, passes in rows:
        bet_id = store.insert("flat_bets", {
            "created_at": "2026-07-20T00:00:00+00:00", "poll_id": 1,
            "poll_kind": "watch", "fingerprint": f"f{pair}{kind}{passes}",
            "position_key": f"p{pair}{kind}{passes}", "kind": kind,
            "category": "sports", "sport": "baseball_mlb", "venue_pair": pair,
            "event_key": "e", "legs_json": "[]", "stake_usd": "1",
            "edge_points": "0.01", "trusted": 0, "lane": "review",
            "quality_flag": "none", "status": "OPEN"})
        for verdicts in passes:
            store.insert("bet_events", {
                "bet_id": bet_id, "created_at": "2026-07-21T00:00:00+00:00",
                "kind": "reconciled",
                "payload_json": json.dumps({"verdicts": verdicts})})

    # 2 mismatch-free complementary pairs; the WON/WON mismatch is excluded
    assert certificate_n(store, "kalshi:polymarket") == 2
    assert certificate_n(store, "kalshi:sx") == 1
    assert certificate_n(store, "polymarket:sx") == 0


def test_certificate_n_is_zero_for_a_pair_with_a_mismatch_only(cfg, tmp_path):
    """A mismatch does not merely fail to count — it is the event the rule of
    three is counting the absence of."""
    import json

    from arb.reconcile import certificate_n
    from arb.store import Store
    store = Store(tmp_path / "t2.sqlite")
    bet_id = store.insert("flat_bets", {
        "created_at": "2026-07-20T00:00:00+00:00", "poll_id": 1,
        "poll_kind": "watch", "fingerprint": "f1", "position_key": "p1",
        "kind": "XSPREAD", "category": "sports", "sport": "baseball_mlb",
        "venue_pair": "kalshi:polymarket", "event_key": "e", "legs_json": "[]",
        "stake_usd": "1", "edge_points": "0.01", "trusted": 0, "lane": "review",
        "quality_flag": "none", "status": "OPEN"})
    store.insert("bet_events", {
        "bet_id": bet_id, "created_at": "2026-07-21T00:00:00+00:00",
        "kind": "reconciled",
        "payload_json": json.dumps({"verdicts": ["LOST", "LOST"]})})
    assert certificate_n(store, "kalshi:polymarket") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_reconcile.py -k certificate -v`
Expected: FAIL with `ImportError: cannot import name 'certificate_n' from 'arb.reconcile'`

- [ ] **Step 3: Write minimal implementation**

Append to `arb/reconcile.py`:

```python
GRADED = ("WON", "LOST", "VOID")


def certificate_n(store, venue_pair: str) -> int:
    """Resolution Equivalence Certificate size (SPEC §12, §18.2): the number of
    XSPREAD positions on this venue pair whose BOTH legs graded and which
    settled complementary. Feeds eligibility's rule-of-three charge
    `(3/n)·LGM`, so it must count only positions that actually demonstrated
    settlement agreement.

    Verdicts are UNIONED across passes: reconcile grades one leg per pass
    (`_reconciled_legs`), so a fully graded pair appears as ["WON", null] in one
    event and [null, "LOST"] in a later one. Reading events independently
    reports it as two half-graded pairs and undercounts the certificate.

    UNRESOLVABLE is missing data, never evidence of agreement — SX returns it
    unconditionally because its settled-status semantics are unverified
    (data/verified_facts.md), so an SX pair can never accumulate a certificate.
    """
    per_bet: dict[int, dict[int, str]] = {}
    for r in store.query(
            "SELECT b.id, e.payload_json FROM flat_bets b JOIN bet_events e "
            "ON e.bet_id = b.id WHERE e.kind = 'reconciled' AND b.kind = 'XSPREAD' "
            "AND b.venue_pair = ? ORDER BY e.id", (venue_pair,)):
        verdicts = (json.loads(r["payload_json"]) or {}).get("verdicts") or []
        slot = per_bet.setdefault(r["id"], {})
        for i, v in enumerate(verdicts):
            if v in GRADED:
                slot[i] = v
    n = 0
    for slot in per_bet.values():
        if len(slot) < 2:
            continue                      # not both legs graded
        if list(slot.values()).count("WON") != 1:
            continue                      # a covering pair pays exactly once
        n += 1
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_reconcile.py -k certificate -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Pin it into the eligibility input**

In `arb/tracker.py`, modify the `classify({...})` call at lines 287-292 to add one key. The call becomes:

```python
        eligible, reason, lane = classify({
            **cand, "pregame": pregame, "micro": False, "ai_matched": False,
            "live": cand.get("live", False),
            # the certificate is a DB fact, so the caller pins it (rule 12) —
            # classify stays a pure function of a flat dict
            "certificate_n": certificate_n(store, cand["venue_pair"])
            if cand["kind"] == "XSPREAD" else 0,
            "pair_demoted": not booking_trusted(cfg, store, cand["venue_pair"])
            if cand["venue_pair"] in [str(p) for p in cfg["trusted_pairs"]] else False,
        }, cfg)
```

Add the import at the top of `arb/tracker.py`, alongside the other `arb` imports:

```python
from arb.reconcile import certificate_n
```

If that import creates a circular import (`reconcile` importing `tracker`), import it inside `process_poll` instead:

```python
    from arb.reconcile import certificate_n
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 364 passed

- [ ] **Step 7: Verify the arithmetic still gates (this must NOT promote anything)**

Run:

```bash
.venv/bin/python - <<'PY'
from decimal import Decimal
from arb.config import load
from arb.reconcile import certificate_n
from arb.store import Store
cfg = load(); s = Store("data/arb.sqlite")
for pair in ("kalshi:polymarket", "kalshi:sx", "polymarket:sx"):
    n = certificate_n(s, pair)
    risk = (Decimal(3) / n * cfg["mismatch_pricing"]["lgm"]) if n else None
    print(f"{pair:<20} n={n:<4} risk={'n/a' if risk is None else f'{float(risk)*100:.2f}pts'}")
PY
```

Expected: `kalshi:polymarket n=41 risk=7.32pts`. Since 7.32pts far exceeds any observed edge (0.5pt median), `net_spread_edge` stays negative and `classify` returns `"net-spread-nonpositive"` — no position promotes. Confirm no new clean-lane rows appear:

Run: `.venv/bin/python -m arb banksim --hours 24`
Expected: clean-lane column still `$0.00 (0)`.

- [ ] **Step 8: Commit**

```bash
git add arb/reconcile.py arb/tracker.py tests/test_reconcile.py
git commit -m "Reconcile: derive certificate_n, the number nothing ever wrote

eligibility.py:77 reads certificate_n and returns 'no-certificate' when it is
0. Nothing in the codebase ever set it, so every XSPREAD was review-lane
forever regardless of configuration — the Resolution Equivalence Certificate
was specified and never built.

certificate_n counts XSPREAD positions on a venue pair whose BOTH legs graded
and which settled complementary. Verdicts are UNIONED across passes because
reconcile grades one leg per pass; reading events independently undercounts.
UNRESOLVABLE is missing data, never agreement — which is why SX can never
accumulate a certificate (its settled-status semantics are unverified).

This makes the gate COMPUTABLE, not passable: at n=41 the rule-of-three charge
is 7.32pts against a 0.5pt median edge, so net_spread_edge stays negative and
nothing promotes. Verified: banksim clean lane still \$0.00.

KNOWN_ISSUES #1."
```

---

### Task 4: Consult the settlement matrix in the trust gate (issue #3)

`arb/eligibility.py:87` gates trust on `cfg["trusted_pairs"]` and never asks the settlement matrix. `settlement_matrix.sport_all_green()` has **zero callers**, so verifying rulebooks — which we began doing on 2026-07-29 — currently cannot affect behaviour. The §18.2 gate is not merely closed; it is disconnected.

This is **restrictive**: it adds a condition. A pair must now be whitelisted **and** have every matrix cell GREEN for the sport.

**Files:**
- Modify: `arb/eligibility.py` — add the matrix check to the `XSPREAD` branch
- Test: `tests/test_eligibility.py`

**Interfaces:**
- Consumes: `arb.settlement_matrix.load_matrix()`, `sport_all_green(matrix, sport)`; `o["sport"]`
- Produces: new reason string `"matrix-not-green"` with lane `"review"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eligibility.py`:

```python
def test_xspread_requires_a_green_settlement_matrix(cfg):
    """eligibility.py gated trust on trusted_pairs and never consulted the
    matrix; sport_all_green had zero callers. Verifying rulebooks could not
    affect behaviour, so the 18.2 gate was disconnected rather than closed.
    tennis.walkover is RED (Kalshi resolves a pre-match cancellation to 'a fair
    price', Polymarket to a hard 50-50 — verified 2026-07-29)."""
    from arb.eligibility import classify
    base = {
        "kind": "XSPREAD", "category": "sports", "sport": "tennis",
        "venue_pair": "kalshi:polymarket", "status": "OK", "quality_flag": "none",
        "live": False, "start_grade": "verified", "pregame": True,
        "leg_skew_secs": 1.0, "depth_ok": True, "edge_points": "0.50",
        "roi": "0.01", "proof_status": None, "ai_matched": False, "micro": False,
        "certificate_n": 1000, "pair_demoted": False,
    }
    c = dict(cfg)
    c["trusted_pairs"] = ["kalshi:polymarket"]
    eligible, reason, lane = classify(base, c)
    assert not eligible
    assert reason == "matrix-not-green"
    assert lane == "review"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_eligibility.py::test_xspread_requires_a_green_settlement_matrix -v`
Expected: FAIL — `reason` is `"xspread-certified"` and `eligible` is `True`, because the matrix is never consulted.

- [ ] **Step 3: Write minimal implementation**

In `arb/eligibility.py`, inside the `if kind == "XSPREAD":` branch, immediately **after** the `pair not in trusted_pairs` check and **before** the `pair_demoted` check, insert:

```python
        # The whitelist says "we intend to trust this pair"; the matrix says
        # "both venues' rulebooks agree for this sport". Without this call
        # sport_all_green had zero callers, so verifying rulebooks could not
        # affect behaviour and the §18.2 gate was disconnected, not closed.
        from arb.settlement_matrix import load_matrix, sport_all_green
        if not sport_all_green(load_matrix(), str(o.get("sport") or "")):
            return False, "matrix-not-green", "review"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_eligibility.py -k matrix -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 365 passed

- [ ] **Step 6: Commit**

```bash
git add arb/eligibility.py tests/test_eligibility.py
git commit -m "Eligibility: consult the settlement matrix, not just the whitelist

sport_all_green had zero callers. eligibility.py gated trust on trusted_pairs
alone, so the settlement-rule verification started on 2026-07-29 had no path to
affecting behaviour — the 18.2 gate was disconnected, not closed.

A pair must now be whitelisted AND have every matrix cell GREEN for the sport.
Restrictive by construction: it adds a condition and can only withhold trust.
tennis.walkover is RED on primary-source evidence (Kalshi resolves a pre-match
cancellation to 'a fair price in accordance with the rules'; Polymarket to a
hard 50-50), so tennis cannot promote even if whitelisted.

KNOWN_ISSUES #3."
```

---

### Task 5: Exclude SX from clean-lane eligibility (issue #5)

SX cannot support a position and should never reach the clean lane:
- **0 of 356,595** SX quotes carry a bid → an SX leg can never be unwound or marked.
- `reconcile._sx` returns `None` unconditionally because settled-status semantics are unverified → SX legs never grade, so an SX pair can never accumulate a certificate.
- Chain deprecation "coming months" (taxonomy class 10, venue death).
- SX legs quoted at **0.001–0.041** produced every top artifact in the ledger (15%–98% "edges").

Yet **both clean-lane positions in the entire ledger are SX**. Config-driven so the operator can re-enable it if SX settlement is ever verified.

**Files:**
- Modify: `config.yaml` (add `clean_lane_excluded_venues`)
- Modify: `arb/config.py` (add the key to `SCHEMA`)
- Modify: `arb/eligibility.py` — check it before the kind-specific gates
- Test: `tests/test_eligibility.py`

**Interfaces:**
- Consumes: `cfg["clean_lane_excluded_venues"]` (`list[str]`), `o["venue_pair"]`
- Produces: new reason string `"venue-excluded"` with lane `"review"`. Applies to **every** kind, including same-venue `ARB` on `sx:self`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eligibility.py`:

```python
def test_sx_never_reaches_the_clean_lane(cfg):
    """0 of 356,595 SX quotes carry a bid, so an SX leg can never be unwound or
    marked; reconcile._sx returns None unconditionally (settled-status semantics
    unverified), so SX legs never grade; and the chain is being deprecated. Yet
    both clean-lane positions in the whole ledger were SX."""
    from arb.eligibility import classify
    base = {
        "kind": "ARB", "category": "sports", "sport": "baseball_mlb",
        "venue_pair": "sx:self", "status": "OK", "quality_flag": "none",
        "live": False, "start_grade": "verified", "pregame": True,
        "leg_skew_secs": 0.5, "depth_ok": True, "edge_points": "0.01",
        "roi": "0.01", "proof_status": None, "ai_matched": False, "micro": False,
    }
    eligible, reason, lane = classify(base, cfg)
    assert not eligible
    assert reason == "venue-excluded"
    assert lane == "review"


def test_a_non_excluded_same_venue_arb_still_reaches_clean(cfg):
    from arb.eligibility import classify
    base = {
        "kind": "ARB", "category": "sports", "sport": "baseball_mlb",
        "venue_pair": "kalshi:self", "status": "OK", "quality_flag": "none",
        "live": False, "start_grade": "verified", "pregame": True,
        "leg_skew_secs": 0.5, "depth_ok": True, "edge_points": "0.01",
        "roi": "0.01", "proof_status": None, "ai_matched": False, "micro": False,
    }
    eligible, reason, lane = classify(base, cfg)
    assert eligible and lane == "clean", f"got {reason}/{lane}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_eligibility.py -k sx_never -v`
Expected: FAIL — SX currently reaches `clean` because `sx:self` is a self-pair and nothing excludes the venue.

- [ ] **Step 3: Add the config key**

In `config.yaml`, immediately after the `trusted_pairs: []` line, add:

```yaml
clean_lane_excluded_venues: [sx]    # a venue that cannot support a position never
                                    #   reaches the clean lane. SX: 0 of 356,595
                                    #   stored quotes carry a bid (no unwind, no
                                    #   markout), reconcile._sx returns None
                                    #   unconditionally because settled-status
                                    #   semantics are unverified (so no certificate
                                    #   can accrue), and the chain is being
                                    #   deprecated. Empty the list to re-enable.
```

In `arb/config.py`, add to `SCHEMA` next to `"trusted_pairs": LIST,`:

```python
    "clean_lane_excluded_venues": LIST,
```

- [ ] **Step 4: Write minimal implementation**

In `arb/eligibility.py`, immediately **before** the `# kind-specific gates` comment, insert:

```python
    # A venue that cannot support a position never reaches clean, whatever the
    # kind — including a same-venue ARB on that venue. Both clean-lane positions
    # in the ledger were SX, which can neither be unwound (no bid on any stored
    # quote) nor graded (reconcile._sx returns None unconditionally).
    excluded = {str(v) for v in cfg["clean_lane_excluded_venues"]}
    if excluded & set(str(o.get("venue_pair") or "").split(":")):
        return False, "venue-excluded", "review"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_eligibility.py -k "sx_never or non_excluded" -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 367 passed. If a pre-existing test asserts an SX row reaches `clean`, update that test — the exclusion is the intended new behaviour, and note it in the commit.

- [ ] **Step 7: Commit**

```bash
git add config.yaml arb/config.py arb/eligibility.py tests/test_eligibility.py
git commit -m "Eligibility: a venue that cannot support a position never reaches clean

Both clean-lane positions in the entire ledger were SX, which cannot support
one: 0 of 356,595 stored SX quotes carry a bid (so no unwind and no markout),
reconcile._sx returns None unconditionally because settled-status semantics are
unverified (so no certificate can ever accrue), and the chain is being
deprecated. SX legs quoted at 0.001-0.041 also produced every top-edge artifact
in the ledger, at 15-98% 'edges'.

Config-driven (clean_lane_excluded_venues) so the operator can re-enable it if
SX settlement is ever verified live. Applies to every kind, including
same-venue ARB on sx:self.

KNOWN_ISSUES #5."
```

---

### Task 6: Update KNOWN_ISSUES to reflect the fixes

**Files:**
- Modify: `KNOWN_ISSUES.md`

- [ ] **Step 1: Move the fixed entries**

Edit `KNOWN_ISSUES.md`: for issues **#1, #3, #5, #6, #8, #28**, append to each entry a line of the form:

```markdown
*FIXED 2026-07-29 (<short-sha>):* <one sentence on what changed and what it did NOT change.>
```

For #1 and #3 the "did not change" clause is load-bearing and must say: *the arithmetic still gates — nothing promotes at n=41.*

- [ ] **Step 2: Re-verify the two headline numbers**

Run: `.venv/bin/python -m arb banksim --hours 24`
Expected: clean-lane column `$0.00 (0)`. Record it. If it is **not** zero, stop — a restrictive change has become permissive and needs investigation before commit.

- [ ] **Step 3: Commit**

```bash
git add KNOWN_ISSUES.md
git commit -m "KNOWN_ISSUES: mark #1, #3, #5, #6, #8, #28 fixed

Six code-level defects closed. None makes the system more permissive: #1 and #3
make the 18.2 gate computable and connected while the rule-of-three arithmetic
still refuses every position at n=41; #5, #6 and #28 only withhold trust; #8 is
additive.

Verified after all six: banksim clean-lane P&L still \$0.00."
```

---

## Self-Review

**1. Spec coverage.** Every code-fixable item in `KNOWN_ISSUES.md` Tiers 1–2 and 6 maps to a task: #1→T3, #3→T4, #5→T5, #6→T2, #8→T1, #28→T2, bookkeeping→T6. #2, #4, #22–25 are findings with nothing to implement; #7 is deferred to its own plan (stated in Out of Scope); #13–18 were fixed before this plan; #19–21 are already reverted; #26–27, #29–30 are operational and unchanged.

**2. Placeholder scan.** No TBD/TODO. Every code step shows complete code. Every test step shows the full test body. Every command shows expected output.

**3. Type consistency.** `certificate_n(store, venue_pair: str) -> int` is defined in T3 Step 3 and consumed in T3 Step 5 with the same name and signature. `sport_all_green(matrix, sport)` and `load_matrix()` match `arb/settlement_matrix.py`. `clean_lane_excluded_venues` is `LIST` in both `config.yaml` and `SCHEMA`. `max_spread_fraction_of_edge` is `NUM` in both. Reason strings (`"matrix-not-green"`, `"venue-excluded"`, `"spread>edge"`, `"no-certificate"`) are each used consistently between test and implementation.

**One risk worth flagging to the operator:** Tasks 4 and 5 tighten the clean lane, and the ledger's only two clean positions are SX. After Task 5 the clean lane will be **empty by construction** rather than empty by circumstance. That is the honest state — but it means the "clean-lane P&L = $0.00" check in T3/T6 can no longer distinguish "correctly refusing" from "accidentally broken". Task 5's second test (`test_a_non_excluded_same_venue_arb_still_reaches_clean`) is the guard against that, and it must stay green.
