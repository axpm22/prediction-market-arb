# Maker Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether *posting* (maker) instead of *crossing* (taker) turns the observed edge positive — the last untested structural direction in the model.

**Architecture:** Record a Polymarket WebSocket book tape into its own SQLite file, then replay it through a pure maker-fill simulator that brackets queue position (conservative = everything ahead of us consumed; optimistic = front of queue). Score each simulated fill by markout against later *executable* prices. Report as a two-sided bracket, never a point estimate.

**Tech Stack:** Python 3.13, `websockets` (optional extra, already installed), SQLite, `Decimal` money math, pytest.

## Why this lane exists

Every other lane in this project dies at the same gate. Measured, not assumed:

| barrier | status |
|---|---|
| settlement divergence | solved — 9 verified pairs share one BLS release |
| detection | works — 2,513 cover observations logged |
| observation speed | solved — PM WS is 156× REST (935 vs 6 updates / 360 s) |
| inter-leg skew | solved — 0.27 s achieved vs a 10 s bound |
| **spread** | **13× the edge on the one fully-measured cover** |

Taking always pays the spread. The only structural escape is to *earn* it instead. `arb/bench_markout.py:l7_maker` already tried and says so itself: it is a **one-sided** bound built from hourly candles, and its own note reads *"a one-sided bracket bounds nothing."* This plan builds the missing side at WebSocket resolution.

## Global Constraints

Copied verbatim from CLAUDE.md / STRATEGIES.md. Every task's requirements include these.

- **Rule 1 — paper trading only.** Public, unauthenticated GETs/POSTs only. **This lane never places an order**; a "fill" here is always a simulated counterfactual.
- **Rule 2 — never spend Odds API credits in tests or loops.** Nothing in this plan touches the Odds API. Polymarket is keyless.
- **Rule 3 — do not invent API endpoints or fields.** Only endpoints in SPEC.md §6 or verified live and dated in `data/verified_facts.md`. Task 1 exists to satisfy this.
- **Rule 4 — no magic numbers.** Every threshold, horizon, and cap lives in `config.yaml`.
- **Rule 5 — money math uses `Decimal`, never float.** Floats permitted only for display and statistical internals, re-quantized to 6-decimal `Decimal`.
- **Rule 9 — `flat_bets` is append-only.** This lane writes to its OWN SQLite file (`data/pm_tape.sqlite`), so rule 9 is structurally unreachable — the same defence `arb/kalshi_history.py` uses.
- **Rule 13 — nothing restarts on a dirty tree.** Loops run only under the supervisor, from a committed HEAD, after a green `pytest`.
- **Measurement rule 1 — executable prices only. Mids are banned.** A markout marks against the price we could actually have exited at (the touch), never a midpoint.
- **Measurement rule 12 — one primary per lane.** The primary is the *conservative* bracket bound. The optimistic bound is context, never the headline.
- Modules stay under ~300 lines.
- Test command is `.venv/bin/python -m pytest -q` — the framework Python lacks `dotenv`.
- `websockets` is an **optional extra** like `anthropic`. It must never be imported at module top level in anything the core imports; import it inside the recorder function.

## File Structure

| File | Responsibility |
|---|---|
| `arb/pm_tape.py` (new, ~180 lines) | Record PM WS frames to `data/pm_tape.sqlite`. I/O only — no analysis. |
| `arb/maker.py` (new, ~200 lines) | Pure simulator: queue-position bracket + markout scoring. No sockets, no DB. |
| `arb/bench_maker.py` (new, ~90 lines) | `LaneResult` wrapper so the lane ranks alongside L1–L11. |
| `tests/test_pm_tape.py` (new) | Schema + frame-persistence, against a fake frame stream. |
| `tests/test_maker.py` (new) | The simulator's failure modes, one test each. |
| `arb/cli.py` (modify) | `arb pm-tape --record-secs N` subcommand. |
| `arb/commands.py` (modify) | `cmd_pm_tape` handler. |
| `arb/bench_lanes.py` (modify) | Register the lane in `LANES`. |
| `config.yaml` (modify) | `maker:` block. |
| `data/verified_facts.md` (modify) | Task 1's dated fill-observability finding. |

The split is deliberate: `maker.py` is pure so it can be tested exhaustively without a network, and `pm_tape.py` is dumb so a protocol surprise can't corrupt analysis logic.

---

### Task 1: Verify how a fill is observable on the PM feed

Rule 3 blocks writing a simulator against a protocol we have not confirmed. We verified `book` and `price_change` frames on 2026-07-30; we did **not** verify whether trades are directly reported. The simulator's fill rule depends on the answer, so this is resolved first and written down.

**Files:**
- Modify: `data/verified_facts.md` (append one row)

- [ ] **Step 1: Probe the live feed for every event type it emits**

Save as `/tmp/verify_fills.py` and run with `PYTHONPATH=$(pwd)`:

```python
import asyncio, json, ssl, certifi, time, collections
import websockets
from arb import config
from arb.venues.base import get_json

cfg = config.load()
ev = get_json("https://gamma-api.polymarket.com/markets?closed=false"
              "&order=volume24hr&ascending=false&limit=40", cfg)
toks = []
for m in ev:
    try:
        t = json.loads(m.get("clobTokenIds") or "[]")
        pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except Exception:
        continue
    if len(t) == 2 and len(pr) == 2 and 0.10 <= pr[0] <= 0.90:
        toks += t
    if len(toks) >= 20:
        break

ctx = ssl.create_default_context(cafile=certifi.where())
kinds = collections.Counter()
samples = {}

async def main():
    async with websockets.connect(
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ssl=ctx, open_timeout=20, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
        end = time.time() + 180
        while time.time() < end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
            except asyncio.TimeoutError:
                break
            for e in (lambda d: d if isinstance(d, list) else [d])(json.loads(msg)):
                et = e.get("event_type")
                kinds[et] += 1
                samples.setdefault(et, e)

asyncio.run(main())
print("event types seen:", dict(kinds))
for k, v in samples.items():
    print(f"\n--- {k} ---")
    print(json.dumps(v, indent=1)[:700])
```

- [ ] **Step 2: Run it and record which of the two worlds we are in**

Run: `PYTHONPATH=$(pwd) .venv/bin/python /tmp/verify_fills.py`

Expected: `event_type` counts, and a sample of each. Decide between:

- **World A — a trade/`last_trade` event type exists.** Fills are observed directly. Note its exact field names.
- **World B — only `book` and `price_change` exist.** Fills must be *inferred* from size decreasing at a price level. This is the expected outcome based on the 2026-07-30 probe.

Task 3 is written for **World B**, because it is the weaker assumption and works in both worlds. If World A holds, Task 3 still works — record the trade fields for a later refinement, but do not change Task 3.

- [ ] **Step 3: Append the dated fact**

Add one row to `data/verified_facts.md`, filling in what Step 2 actually printed:

```
| Polymarket WS fill observability | **VERIFIED live 2026-07-30.** Event types observed over 180 s on 20 contested liquid tokens: `<paste the dict from Step 2>`. <If only book/price_change: "No trade event type is emitted, so a maker fill must be INFERRED from size decreasing at a price level (World B); the simulator brackets queue position rather than claiming a fill it cannot see."> Frame shapes already dated above | probe 2026-07-30 |
```

- [ ] **Step 4: Commit**

```bash
git add data/verified_facts.md
git commit -m "verified_facts: how a maker fill is observable on the PM feed

Rule 3 gate for the maker lane: the simulator's fill rule depends on
whether trades are reported directly or must be inferred from book
deltas. Recorded before any simulator code exists."
```

---

### Task 2: Record the PM WebSocket tape to its own SQLite

**Files:**
- Create: `arb/pm_tape.py`
- Create: `tests/test_pm_tape.py`
- Modify: `config.yaml`

**Interfaces:**
- Consumes: Task 1's confirmation that `book` and `price_change` are the frame types.
- Produces:
  - `SCHEMA: str`
  - `open_tape(path: str | Path) -> sqlite3.Connection`
  - `persist_frames(con, frames: list[dict], received_at: list[float]) -> int`
  - `record(cfg, tokens: list[str], secs: int) -> dict` (live; needs `websockets`)

- [ ] **Step 1: Add the config block**

In `config.yaml`, immediately after the `bench:` block, add:

```yaml
maker:                              # maker-side measurement lane (2026-07-30). Taking always
                                    #   pays the spread, which is 13x the edge on the one
                                    #   fully-measured cover; this lane measures EARNING it.
  tape_db: data/pm_tape.sqlite      # own SQLite file: rule 9 cannot be reached from here
  record_secs: 3600                 # one recording session's wall clock
  max_tokens: 60                    # WS subscription width; 50 tokens produced ~108k frames
                                    #   in 7 minutes, so this bounds tape size not coverage
  min_price: 0.10                   # contested markets only: a book at 0.002/0.998 is
  max_price: 0.90                   #   resolved and can never produce a maker fill
  markout_horizons_secs: [30, 120, 600]
  min_fills_for_verdict: 100        # below this the bracket is reported but never scored
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_pm_tape.py`:

```python
"""PM WebSocket tape recorder. Its OWN SQLite file (rule 9 unreachable).
No sockets in tests — frames are fed as dicts."""
import sqlite3

from arb import pm_tape

BOOK = {"event_type": "book", "asset_id": "tokA", "timestamp": "1785457058983",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "80"}]}
PC = {"event_type": "price_change", "timestamp": "1785457066528",
      "price_changes": [{"asset_id": "tokA", "price": "0.41", "size": "5",
                         "side": "BUY", "best_bid": "0.41", "best_ask": "0.42"}]}


def test_schema_creates_in_a_standalone_file(tmp_path):
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tape_frames" in names
    # rule 9: nothing in this file can reach the ledger
    assert "flat_bets" not in names and "bet_events" not in names


def test_persist_stores_one_row_per_frame_with_receipt_time(tmp_path):
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    n = pm_tape.persist_frames(con, [BOOK, PC], [1000.5, 1001.25])
    assert n == 2
    rows = [dict(r) for r in con.execute(
        "SELECT event_type, received_at, asset_id FROM tape_frames ORDER BY id")]
    assert [r["event_type"] for r in rows] == ["book", "price_change"]
    assert rows[0]["received_at"] == 1000.5
    assert rows[0]["asset_id"] == "tokA"


def test_price_change_asset_id_is_read_from_the_change_not_the_root(tmp_path):
    """price_change frames carry no root asset_id — it lives on each entry of
    price_changes[]. Reading the root drops the id and the replay cannot key
    the frame to a book."""
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    pm_tape.persist_frames(con, [PC], [1.0])
    got = con.execute("SELECT asset_id FROM tape_frames").fetchone()[0]
    assert got == "tokA"


def test_raw_json_round_trips_so_replay_never_depends_on_the_parser(tmp_path):
    import json
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    pm_tape.persist_frames(con, [BOOK], [1.0])
    raw = con.execute("SELECT raw_json FROM tape_frames").fetchone()[0]
    assert json.loads(raw) == BOOK


def test_persist_is_resumable_across_connections(tmp_path):
    p = tmp_path / "t.sqlite"
    pm_tape.persist_frames(pm_tape.open_tape(p), [BOOK], [1.0])
    pm_tape.persist_frames(pm_tape.open_tape(p), [PC], [2.0])
    con = sqlite3.connect(p)
    assert con.execute("SELECT COUNT(*) FROM tape_frames").fetchone()[0] == 2
```

- [ ] **Step 3: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_pm_tape.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'arb.pm_tape'`

- [ ] **Step 4: Write the implementation**

Create `arb/pm_tape.py`:

```python
"""Polymarket WebSocket book tape -> its own SQLite file.

Rule 1: public, unauthenticated, read-only market data. Nothing here places
        an order; the maker lane's "fills" are simulated counterfactuals.
Rule 3: the endpoint and frame shapes were verified live 2026-07-30 and dated
        in data/verified_facts.md BEFORE this module was written.
Rule 9: writes to data/pm_tape.sqlite, so flat_bets is structurally
        unreachable from this lane — the defence arb/kalshi_history.py uses.

This module is deliberately dumb: it records frames and does not interpret
them. All judgement lives in arb/maker.py, which never touches a socket.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from arb.logutil import get_logger

log = get_logger("arb.pm_tape", "logs/scan.log")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tape_frames (
    id INTEGER PRIMARY KEY,
    received_at REAL,        -- OUR clock: the venue timestamp cannot be trusted
                             -- for latency work and is kept separately
    venue_ts TEXT,
    event_type TEXT,
    asset_id TEXT,
    raw_json TEXT);
CREATE INDEX IF NOT EXISTS idx_tape_asset ON tape_frames(asset_id, received_at);
"""


def open_tape(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    con.commit()
    return con


def _asset_of(frame: dict) -> str | None:
    """price_change frames carry NO root asset_id — it lives on each entry of
    price_changes[]. Reading the root yields None and the replay can no longer
    key the frame to a book (this exact mistake parsed 4 of 935 updates)."""
    if frame.get("asset_id"):
        return str(frame["asset_id"])
    for ch in frame.get("price_changes") or []:
        if ch.get("asset_id"):
            return str(ch["asset_id"])
    return None


def persist_frames(con, frames: list[dict], received_at: list[float]) -> int:
    rows = [(rt, str(f.get("timestamp") or ""), str(f.get("event_type") or ""),
             _asset_of(f), json.dumps(f))
            for f, rt in zip(frames, received_at)]
    con.executemany(
        "INSERT INTO tape_frames (received_at, venue_ts, event_type, asset_id,"
        " raw_json) VALUES (?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def contested_tokens(cfg) -> list[str]:
    """Liquid markets whose price is not already resolved. A book at
    0.002/0.998 cannot produce a maker fill, so recording it is dead weight."""
    from arb.venues.base import get_json
    lo = float(cfg["maker"]["min_price"])
    hi = float(cfg["maker"]["max_price"])
    cap = int(cfg["maker"]["max_tokens"])
    ev = get_json("https://gamma-api.polymarket.com/markets?closed=false"
                  "&order=volume24hr&ascending=false&limit=200", cfg)
    out: list[str] = []
    for m in ev:
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except (TypeError, ValueError):
            continue
        if len(toks) == 2 and len(prices) == 2 and lo <= prices[0] <= hi:
            out += [str(t) for t in toks]
        if len(out) >= cap:
            break
    return out[:cap]


def record(cfg, tokens: list[str], secs: int) -> dict:
    """Live recorder. `websockets` is an OPTIONAL extra — imported here, never
    at module top level, so core imports keep working without it."""
    import asyncio
    import ssl

    try:
        import certifi
        import websockets
    except ImportError as e:
        raise RuntimeError(
            "the maker tape needs the optional websockets package: "
            ".venv/bin/pip install websockets") from e

    con = open_tape(cfg["maker"]["tape_db"])
    ctx = ssl.create_default_context(cafile=certifi.where())
    stats = {"frames": 0, "batches": 0}

    async def main():
        async with websockets.connect(WS_URL, ssl=ctx, open_timeout=20,
                                      max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
            end = time.time() + secs
            buf, times = [], []
            while time.time() < end:
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    break
                except Exception as e:  # noqa: BLE001 — a drop is data, not a crash
                    log.warning("pm tape socket closed: %s", type(e).__name__)
                    break
                now = time.time()
                data = json.loads(msg)
                for f in (data if isinstance(data, list) else [data]):
                    buf.append(f)
                    times.append(now)
                if len(buf) >= 500:      # batch: one commit per frame is slower
                    stats["frames"] += persist_frames(con, buf, times)
                    stats["batches"] += 1
                    buf, times = [], []
            if buf:
                stats["frames"] += persist_frames(con, buf, times)
                stats["batches"] += 1

    asyncio.run(main())
    stats["tokens"] = len(tokens)
    log.info("pm tape: %s", stats)
    return stats
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pm_tape.py -q`
Expected: `5 passed`

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests pass (398 + 5 = 403 at time of writing)

- [ ] **Step 7: Commit**

```bash
git add arb/pm_tape.py tests/test_pm_tape.py config.yaml
git commit -m "pm_tape: record the PM WebSocket book tape to its own SQLite

Own file (data/pm_tape.sqlite), so rule 9 is structurally unreachable —
the same defence kalshi_history.py uses. The recorder is deliberately
dumb: it stores frames and never interprets them, so a protocol surprise
cannot corrupt analysis. websockets stays an optional extra, imported
inside record() rather than at module top level.

price_change frames carry no root asset_id; _asset_of reads it from the
change entry. Reading the root is what parsed 4 of 935 updates once."
```

---

### Task 3: The maker-fill simulator with a queue-position bracket

The measurement problem: we cannot know our place in the queue, so we cannot know whether a given trade would have filled *us*. Claiming a fill we cannot prove is how L7 got a bound that "bounds nothing." This brackets it instead — the conservative bound assumes we are last in queue, the optimistic assumes first, and the truth is between.

**Files:**
- Create: `arb/maker.py`
- Create: `tests/test_maker.py`

**Interfaces:**
- Consumes: `arb.pm_tape.open_tape` (to read a recorded tape).
- Produces:
  - `RestingOrder` dataclass: `(token: str, price: Decimal, queue_ahead: Decimal, posted_at: float)`
  - `MakerSim.__init__(fee_rate: Decimal)`
  - `MakerSim.post(token, price, queue_ahead, posted_at) -> RestingOrder`
  - `MakerSim.observe(token, price, size, at) -> list[dict]` — returns fill dicts
  - `Fill` keys: `{"token", "price", "at", "basis"}` where `basis` is `"conservative"` or `"optimistic"`

- [ ] **Step 1: Write the failing test**

Create `tests/test_maker.py`:

```python
"""Maker-fill simulator. Pure — no sockets, no DB, no clock.

The whole point is the BRACKET: we cannot observe our queue position, so a
single fill number is a claim we cannot support. Conservative assumes we are
last in queue; optimistic assumes first.
"""
from decimal import Decimal

import pytest

from arb.maker import MakerSim


def _sim(fee="0"):
    return MakerSim(fee_rate=Decimal(fee))


def test_no_fill_before_anything_trades_at_our_price():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    assert s.observe("tokA", Decimal("0.40"), Decimal("0"), at=1.0) == []


def test_optimistic_fills_on_the_first_size_at_our_price():
    """Front of queue: the first contract traded at our price is ours."""
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    fills = s.observe("tokA", Decimal("0.40"), Decimal("10"), at=1.0)
    assert [f["basis"] for f in fills] == ["optimistic"]
    assert fills[0]["price"] == Decimal("0.40")


def test_conservative_waits_until_the_whole_queue_ahead_is_consumed():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    s.observe("tokA", Decimal("0.40"), Decimal("60"), at=1.0)
    assert s.observe("tokA", Decimal("0.40"), Decimal("30"), at=2.0) == []
    fills = s.observe("tokA", Decimal("0.40"), Decimal("20"), at=3.0)
    assert [f["basis"] for f in fills] == ["conservative"]
    assert fills[0]["at"] == 3.0


def test_each_basis_fills_at_most_once():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("1"), posted_at=0.0)
    first = s.observe("tokA", Decimal("0.40"), Decimal("500"), at=1.0)
    assert sorted(f["basis"] for f in first) == ["conservative", "optimistic"]
    assert s.observe("tokA", Decimal("0.40"), Decimal("500"), at=2.0) == []


def test_size_at_a_worse_price_never_fills_our_bid():
    """We bid 0.40. Trades at 0.41 are someone else's; only 0.40-or-better
    reaches our resting buy."""
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("0"), posted_at=0.0)
    assert s.observe("tokA", Decimal("0.41"), Decimal("999"), at=1.0) == []
    assert s.observe("tokA", Decimal("0.40"), Decimal("1"), at=2.0) != []


def test_another_token_never_fills_our_order():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("0"), posted_at=0.0)
    assert s.observe("tokB", Decimal("0.40"), Decimal("999"), at=1.0) == []


def test_markout_marks_against_an_executable_exit_never_a_mid():
    """Measurement rule 1: mids are banned. A maker BUY exits by selling into
    the bid, so the bid is the honest mark."""
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "conservative"}
    got = markout(fill, exit_bid=Decimal("0.43"), fee=Decimal("0.005"))
    assert got == Decimal("0.025")           # 0.43 - 0.40 - 0.005


def test_markout_is_negative_when_the_book_moves_against_the_fill():
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "conservative"}
    assert markout(fill, exit_bid=Decimal("0.37"),
                   fee=Decimal("0.005")) == Decimal("-0.035")


def test_fee_is_charged_on_every_fill():
    """A maker rebate is not free money — the fee still has to clear."""
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "optimistic"}
    assert markout(fill, exit_bid=Decimal("0.40"),
                   fee=Decimal("0.01")) == Decimal("-0.01")


def test_bracket_reports_both_bounds_and_names_the_primary():
    from arb.maker import bracket
    fills = [{"basis": "conservative", "markout": Decimal("-0.01")},
             {"basis": "conservative", "markout": Decimal("0.03")},
             {"basis": "optimistic", "markout": Decimal("0.05")}]
    b = bracket(fills)
    assert b["conservative"]["n"] == 2
    assert b["conservative"]["mean"] == Decimal("0.01")
    assert b["optimistic"]["n"] == 1
    assert b["primary"] == "conservative"


def test_bracket_on_no_fills_is_empty_not_zero():
    """Zero mean from zero fills reads as 'measured, no edge'. It is not
    measured at all."""
    from arb.maker import bracket
    b = bracket([])
    assert b["conservative"]["n"] == 0
    assert b["conservative"]["mean"] is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_maker.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'arb.maker'`

- [ ] **Step 3: Write the implementation**

Create `arb/maker.py`:

```python
"""Maker-fill simulator: what a RESTING order would have earned.

Taking always pays the spread, and on the one fully-measured cover the spread
was 13x the edge. The only structural escape is to earn the spread instead of
paying it, which means posting rather than crossing.

The honest difficulty is queue position. We cannot observe where in the queue
a hypothetical order would sit, so a single fill count is a claim we cannot
support — arb/bench_markout.py:l7_maker says as much about its own one-sided
number. This module brackets it:

    conservative  we are LAST in queue: filled only after every contract
                  resting ahead of us at our price has traded
    optimistic    we are FIRST in queue: the first contract at our price is ours

The truth is between. The conservative bound is the pre-registered primary
(measurement rule 12); the optimistic bound is context and never the headline.

Pure by construction: no sockets, no database, no clock. Everything arrives as
an argument so every failure mode is unit-testable.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from arb.models import q6


@dataclass
class RestingOrder:
    token: str
    price: Decimal
    queue_ahead: Decimal        # contracts resting ahead of us when we posted
    posted_at: float
    consumed: Decimal = Decimal(0)
    filled: set = field(default_factory=set)


class MakerSim:
    """One resting BUY per token. Feed it observed trade size at a price."""

    def __init__(self, fee_rate: Decimal = Decimal(0)):
        self.fee_rate = Decimal(str(fee_rate))
        self.orders: dict[str, RestingOrder] = {}

    def post(self, token, price, queue_ahead, posted_at) -> RestingOrder:
        o = RestingOrder(str(token), Decimal(str(price)),
                         Decimal(str(queue_ahead)), float(posted_at))
        self.orders[o.token] = o
        return o

    def observe(self, token, price, size, at) -> list[dict]:
        """Size traded at `price` on `token`. Returns any fills it triggers.

        A resting BUY at P is only reachable by trades at P or better (lower);
        size printed at a worse price belongs to someone else's order.
        """
        o = self.orders.get(str(token))
        if o is None:
            return []
        price, size = Decimal(str(price)), Decimal(str(size))
        if size <= 0 or price > o.price:
            return []
        o.consumed += size
        out = []
        if "optimistic" not in o.filled:
            o.filled.add("optimistic")
            out.append({"token": o.token, "price": o.price, "at": float(at),
                        "basis": "optimistic"})
        if "conservative" not in o.filled and o.consumed > o.queue_ahead:
            o.filled.add("conservative")
            out.append({"token": o.token, "price": o.price, "at": float(at),
                        "basis": "conservative"})
        return out


def markout(fill: dict, exit_bid: Decimal, fee: Decimal) -> Decimal:
    """P&L per contract for a maker BUY, marked at an EXECUTABLE exit.

    Measurement rule 1 bans mids. A maker buy is unwound by selling into the
    bid, so the bid is what we could actually have got — marking at a midpoint
    would credit us half a spread nobody offered.
    """
    return q6(Decimal(str(exit_bid)) - Decimal(str(fill["price"]))
              - Decimal(str(fee)))


def _stats(vals: list[Decimal]) -> dict:
    if not vals:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    fs = [float(v) for v in vals]
    mean = statistics.fmean(fs)
    sd = statistics.stdev(fs) if len(fs) > 1 else 0.0
    half = 1.96 * sd / (len(fs) ** 0.5)
    return {"n": len(fs), "mean": q6(Decimal(mean)),
            "lo": q6(Decimal(mean - half)), "hi": q6(Decimal(mean + half))}


def bracket(fills: list[dict]) -> dict:
    """Both bounds, with the primary named. Never collapse this to one number:
    the gap between the bounds IS the queue-position uncertainty."""
    by = {"conservative": [], "optimistic": []}
    for f in fills:
        if f.get("basis") in by and f.get("markout") is not None:
            by[f["basis"]].append(Decimal(str(f["markout"])))
    return {"conservative": _stats(by["conservative"]),
            "optimistic": _stats(by["optimistic"]),
            "primary": "conservative"}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_maker.py -q`
Expected: `11 passed`

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add arb/maker.py tests/test_maker.py
git commit -m "maker: fill simulator with an explicit queue-position bracket

We cannot observe queue position, so a single fill number is a claim we
cannot support — l7_maker's own note says a one-sided bound bounds
nothing. Conservative assumes last in queue, optimistic assumes first,
and the gap between them IS the uncertainty.

Markout marks against the bid, not a mid (measurement rule 1): a maker
buy unwinds by selling into the bid, and a midpoint mark would credit
half a spread nobody offered. Empty input returns n=0 with mean None,
never 0.0 — zero-from-nothing reads as 'measured, no edge'."
```

---

### Task 4: Replay a recorded tape through the simulator

**Files:**
- Create: `arb/bench_maker.py`
- Modify: `arb/bench_lanes.py` (the `LANES` registry at the bottom)
- Modify: `tests/test_maker.py` (append the replay tests)

**Interfaces:**
- Consumes: `MakerSim`, `markout`, `bracket` from Task 3; `open_tape` from Task 2.
- Produces: `replay(cfg, tape_con) -> dict` and `l12_maker(cfg, con, hist) -> LaneResult`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_maker.py`:

```python
def test_replay_infers_fills_from_size_leaving_a_price_level(tmp_path):
    """World B (verified 2026-07-30): no trade event is emitted, so a fill is
    INFERRED from size decreasing at a level. A level whose size GROWS is new
    liquidity joining behind us and must never count as a fill."""
    from arb import bench_maker, pm_tape
    from arb.config import load
    from pathlib import Path
    cfg = load(Path(__file__).resolve().parent.parent / "config.yaml")
    con = pm_tape.open_tape(tmp_path / "t.sqlite")

    def book(sz, ts):
        return {"event_type": "book", "asset_id": "tokA", "timestamp": str(ts),
                "bids": [{"price": "0.40", "size": str(sz)}],
                "asks": [{"price": "0.42", "size": "50"}]}

    # 100 resting -> 130 (liquidity joins, NOT a fill) -> 30 (100 consumed)
    pm_tape.persist_frames(con, [book(100, 1), book(130, 2), book(30, 3)],
                           [1.0, 2.0, 3.0])
    res = bench_maker.replay(cfg, con)
    assert res["consumed_events"] == 1, "growth must not count as consumption"
    assert res["fills"] >= 1


def test_replay_on_an_empty_tape_reports_nothing_measured(tmp_path):
    from arb import bench_maker, pm_tape
    from arb.config import load
    from pathlib import Path
    cfg = load(Path(__file__).resolve().parent.parent / "config.yaml")
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    res = bench_maker.replay(cfg, con)
    assert res["fills"] == 0
    assert res["bracket"]["conservative"]["mean"] is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_maker.py -q -k replay`
Expected: FAIL with `ModuleNotFoundError: No module named 'arb.bench_maker'`

- [ ] **Step 3: Write the implementation**

Create `arb/bench_maker.py`:

```python
"""L12 — maker bracket from the Polymarket WS tape.

Completes what bench_markout.l7_maker could only half-do. L7 works from HOURLY
Kalshi candles and can only see the conservative side, which is why its own
note reads "a one-sided bracket bounds nothing". At WebSocket resolution both
sides are reachable, so this lane reports a real bracket.

Fill inference (World B, verified 2026-07-30): the feed emits no trade event,
so consumption is inferred from size DECREASING at a price level. Size growing
is new liquidity joining the queue behind us — counting that as a fill would
manufacture fills out of other people's orders.
"""
from __future__ import annotations

import json
from decimal import Decimal

from arb.bench import LaneResult, verdict_for
from arb.maker import MakerSim, bracket, markout
from arb.models import q6
from arb.venues.fees import leg_fee_cost


def _touch(frame: dict) -> tuple[Decimal, Decimal] | None:
    """(best_bid_price, size_at_that_price) from a `book` frame, or None."""
    best = None
    for lv in frame.get("bids") or []:
        try:
            p, s = Decimal(str(lv["price"])), Decimal(str(lv["size"]))
        except (KeyError, TypeError, ArithmeticError):
            continue
        if not (0 < p < 1) or s <= 0:
            continue                    # sentinel/empty-book prices are not quotes
        if best is None or p > best[0]:
            best = (p, s)
    return best


def replay(cfg, tape_con) -> dict:
    """Walk the tape once per token, posting at the touch and marking out."""
    fee_rate = (leg_fee_cost(cfg, "polymarket", "politics", Decimal("0.5"),
                             Decimal(1), "paper", "maker") / Decimal("0.5"))
    horizons = [int(h) for h in cfg["maker"]["markout_horizons_secs"]]
    sim = MakerSim(fee_rate=fee_rate)
    last: dict[str, tuple[Decimal, Decimal]] = {}     # token -> (price, size)
    open_fills: list[dict] = []
    done: list[dict] = []
    bids_by_token: dict[str, list[tuple[float, Decimal]]] = {}
    consumed_events = 0

    rows = tape_con.execute(
        "SELECT received_at, asset_id, event_type, raw_json FROM tape_frames "
        "WHERE event_type='book' ORDER BY received_at")
    for r in rows:
        tok = r["asset_id"]
        if not tok:
            continue
        frame = json.loads(r["raw_json"])
        touch = _touch(frame)
        if touch is None:
            continue
        price, size = touch
        at = float(r["received_at"])
        bids_by_token.setdefault(tok, []).append((at, price))
        prev = last.get(tok)
        if prev is not None and prev[0] == price and size < prev[1]:
            # size LEFT this level at an unchanged price: it was consumed.
            # size growing is liquidity joining behind us, never a fill.
            consumed_events += 1
            for f in sim.observe(tok, price, prev[1] - size, at):
                open_fills.append(f)
        if tok not in sim.orders:
            sim.post(tok, price, queue_ahead=size, posted_at=at)
        last[tok] = (price, size)

    # mark every fill out at each horizon against a later EXECUTABLE bid
    for f in open_fills:
        series = bids_by_token.get(f["token"]) or []
        for h in horizons:
            target = f["at"] + h
            later = [p for (t, p) in series if t >= target]
            if not later:
                continue          # right-censored: no mark, never a zero
            fee = leg_fee_cost(cfg, "polymarket", "politics", f["price"],
                               Decimal(1), "paper", "maker")
            done.append({**f, "horizon": h,
                         "markout": markout(f, later[0], fee)})
    return {"fills": len(open_fills), "marked": len(done),
            "consumed_events": consumed_events,
            "tokens": len(last), "bracket": bracket(done)}


def l12_maker(cfg, con, hist) -> LaneResult:
    """LaneResult wrapper so the maker bracket ranks beside L1-L11."""
    from pathlib import Path

    from arb.pm_tape import open_tape

    r = LaneResult(lane="L12", name="maker bracket (PM WS tape)",
                   primary="conservative-queue maker markout per contract",
                   unit="simulated fill")
    path = Path(str(cfg["maker"]["tape_db"]))
    if not path.exists():
        r.note = ("no tape recorded: run `arb pm-tape --record-secs 3600` "
                  "(keyless, 0 credits)")
        return r
    res = replay(cfg, open_tape(path))
    cons = res["bracket"]["conservative"]
    opt = res["bracket"]["optimistic"]
    r.n = int(cons["n"])
    r.estimate, r.lo, r.hi = cons["mean"], cons["lo"], cons["hi"]
    r.detail.append(f"{res['fills']} simulated fills over {res['tokens']} tokens; "
                    f"{res['consumed_events']} consumption events")
    r.detail.append(f"optimistic bound: n={opt['n']} mean={opt['mean']}")
    r.note = ("bracket, not a point estimate — queue position is unobservable, "
              "so the primary assumes we are LAST in queue and the optimistic "
              "bound assumes first; the truth is between")
    if r.n < int(cfg["maker"]["min_fills_for_verdict"]):
        r.note += f"; under min_fills_for_verdict ({cfg['maker']['min_fills_for_verdict']})"
        return r
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r
```

- [ ] **Step 4: Register the lane**

In `arb/bench_lanes.py`, add the import directly below line 20 (`from arb.bench_markout import l7_maker, l11_prints`), keeping the existing alphabetical grouping:

```python
from arb.bench_maker import l12_maker
```

Then, in the `LANES` dict at the bottom of the file, add this entry after the `"L11"` line:

```python
    "L12": ("maker bracket (PM WS tape)", l12_maker),
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_maker.py -q`
Expected: `13 passed`

Run: `.venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add arb/bench_maker.py arb/bench_lanes.py tests/test_maker.py
git commit -m "bench: L12 maker bracket from the PM WebSocket tape

Completes what l7_maker could only half-do — L7 reads HOURLY candles and
can only see the conservative side, hence its own 'a one-sided bracket
bounds nothing'. At WS resolution both sides are reachable.

Fill inference is World B (verified 2026-07-30: no trade event is
emitted): consumption is size DECREASING at an unchanged price level.
Size growing is liquidity joining the queue behind us, and counting that
would manufacture fills out of other people's orders.

Unmarked fills are right-censored and dropped, never marked zero."
```

---

### Task 5: CLI command and a real recording

**Files:**
- Modify: `arb/cli.py`
- Modify: `arb/commands.py`

**Interfaces:**
- Consumes: `pm_tape.record`, `pm_tape.contested_tokens`, `bench_maker.replay`.
- Produces: `arb pm-tape --record-secs N`.

- [ ] **Step 1: Add the subcommand**

In `arb/cli.py`, immediately before the line beginning `p_bench = sub.add_parser("bench"`, add:

```python
    p_tape = sub.add_parser("pm-tape", help="record the Polymarket WS book tape "
                                            "for the maker lane (keyless, 0 credits)")
    p_tape.add_argument("--record-secs", type=int, default=None,
                        help="override maker.record_secs")
```

- [ ] **Step 2: Add the handler**

Append to `arb/commands.py`:

```python
def cmd_pm_tape(cfg, store, args):
    """Record the PM WebSocket book tape into its OWN SQLite (rule 9
    unreachable), then print what the maker bracket makes of it."""
    from arb import bench_maker, pm_tape
    secs = int(args.record_secs or cfg["maker"]["record_secs"])
    tokens = pm_tape.contested_tokens(cfg)
    print(f"recording {len(tokens)} contested tokens for {secs}s -> "
          f"{cfg['maker']['tape_db']}")
    stats = pm_tape.record(cfg, tokens, secs)
    print(f"  frames={stats['frames']} batches={stats['batches']}")
    res = bench_maker.replay(cfg, pm_tape.open_tape(cfg["maker"]["tape_db"]))
    print(f"  simulated fills={res['fills']} marked={res['marked']} "
          f"consumption events={res['consumed_events']}")
    b = res["bracket"]
    for basis in ("conservative", "optimistic"):
        s = b[basis]
        star = "  <- primary" if basis == b["primary"] else ""
        print(f"    {basis:13} n={s['n']:>6} mean={s['mean']} "
              f"[{s['lo']}, {s['hi']}]{star}")
```

- [ ] **Step 3: Verify the command is wired**

Run: `.venv/bin/python -m arb pm-tape --help`
Expected: usage text showing `--record-secs`

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 5: Commit before recording (rule 13)**

```bash
git add arb/cli.py arb/commands.py
git commit -m "cli: arb pm-tape — record the maker tape and print the bracket"
```

- [ ] **Step 6: Record a real 30-minute tape and read the bracket**

Run:

```bash
.venv/bin/python -m arb pm-tape --record-secs 1800
```

Expected shape (numbers will differ):

```
recording 60 contested tokens for 1800s -> data/pm_tape.sqlite
  frames=... batches=...
  simulated fills=... marked=... consumption events=...
    conservative  n=...  mean=...  [..., ...]  <- primary
    optimistic    n=...  mean=...  [..., ...]
```

**How to read it, and the trap to avoid:**

- The **conservative** bound is the answer. If its CI excludes zero on the positive side, posting earns more than it costs and this is the first positive lane in the project.
- If conservative is negative and optimistic is positive, the lane is **undetermined** — it depends entirely on queue position, which we cannot observe and therefore cannot claim. Report it that way. Do not quote the optimistic number as the result.
- If **both** bounds are negative, the maker route is closed too, and the project's conclusion is that neither side of the spread pays. That is a real finding, not a failure.
- `n` below `maker.min_fills_for_verdict` (100) means no verdict at all. Record a longer tape rather than reading an underpowered mean.

- [ ] **Step 7: Record the outcome in the plan file**

Append the actual printed bracket to the bottom of this plan document under a `## Result` heading, with the date and the tape's frame count, so the number is reproducible and dated.

```bash
git add docs/superpowers/plans/2026-07-30-maker-lane.md data/pm_tape.sqlite
git commit -m "maker lane: first recorded bracket

<paste the printed bracket here>"
```

Note: if `data/pm_tape.sqlite` is large, add it to `.gitignore` instead of committing it and record only the printed numbers.

---

## Self-Review

**1. Spec coverage.** The goal is "does posting instead of crossing turn the edge positive." Task 1 satisfies rule 3 for the feed; Task 2 captures the data; Task 3 simulates fills with honest uncertainty; Task 4 turns fills into a scored bracket and ranks it beside the other lanes; Task 5 makes it runnable and interprets the output. No requirement is unassigned.

**2. Placeholder scan.** Every code step contains complete runnable code. The only intentionally deferred content is Task 1 Step 3's `<paste ...>`, which is a *measurement* that cannot exist before the probe runs, and Task 5 Step 7's result paste — both are outputs to record, not logic to invent.

**3. Type consistency.** `MakerSim.post(token, price, queue_ahead, posted_at)` and `MakerSim.observe(token, price, size, at)` are used with those exact names in `bench_maker.replay`. Fill dicts carry `token/price/at/basis` throughout, gaining `markout` and `horizon` only in `replay`. `bracket()` reads `basis` and `markout`, both of which `replay` sets. `open_tape`/`persist_frames`/`contested_tokens`/`record` are used in Tasks 4 and 5 exactly as defined in Task 2.

**Known limitation, stated rather than hidden.** Fill inference cannot distinguish a trade from an order *cancellation* — both remove size from a level. This inflates the fill count in both bounds, and therefore makes the lane optimistic in a way the bracket does not capture. Cancellations are unobservable on a public feed without per-order data, so this cannot be fixed here; if the conservative bound comes out positive, that result must carry this caveat explicitly and should not be trusted until a venue with public trade prints (Kalshi `GET /markets/trades`, SPEC.md §437, still unverified per rule 3) corroborates it.
