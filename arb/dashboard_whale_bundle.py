"""The shared whale-state bundle behind the dashboard's three tape-heavy
endpoints (Task 12 follow-up; live incident 2026-09-05 ~11:00Z).

Each of /api/whale, /api/whale/traders and /api/positions used to run its OWN
`score_state` pass over the print tape (~30s on the live 764k-print tape,
arb/whale_state.py) — dashboard_cache.py's per-endpoint memo did not stop
that, because a memo keyed by endpoint name still recomputes once PER
ENDPOINT. The dashboard polls all three every `dashboard.refresh_secs`, so a
live refresh ran three (or four, /api/positions was never memoised at all)
concurrent 30s CPU burns at once: 231% CPU, 12 open connections, /api/whale
not answering inside 180s, and the whale lane's own 60s cycle — sharing that
CPU — falling behind.

`bundle()` runs `score_state` ONCE (reusing dashboard_cache's existing
single-flight TTL memo — no new locking) and hands back everything the three
endpoints derive their payload from:
  - `state`: the WhaleState itself (arb/whale_state.py).
  - `frame`: the daily report's own frame, built from that SAME state
    (`report_whale._frame_from_state`) — so /api/whale can never disagree
    with `arb report` about who is qualifying.
  - `profiles`: `whale_rank.profiles(state, ...)` — the shared input behind
    /api/whale/traders' rank / wallet_profile / best_markets views. Deriving
    a level/wallet-scoped slice of an already-built `profiles` is cheap, so
    (unlike the old per-endpoint memo) a wallet-scoped lookup no longer needs
    its own cache entry — `cache_max_entries` now only bounds how many
    distinct bundles (one per refresh where something actually changed) this
    process keeps around.

Keyed by three cheap MAX() probes rather than wall-clock alone: a refresh
where nothing changed is a pure cache hit for the rest of `cache_secs`
(matching the module doc of arb/dashboard_cache.py), and a new print,
holdings poll or booked bet lands a NEW key immediately — no stale-vs-live
race to reason about, and the old key's entry just ages out of the LRU.

report_whale.whale_frame (the daily report's only entry point) never calls
into this module and never touches the cache — see its own docstring."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from arb import dashboard_cache, report_whale, whale_rank
from arb.whale_state import score_state


def _probe_key(cfg, store) -> tuple:
    """Three cheap SELECT MAX(...)s. `prints.id` and `flat_bets.id` are each
    table's own INTEGER PRIMARY KEY (rowid) — SQLite answers MAX() on that
    from the last rowid, no scan. `holdings.observed_at` is not a rowid and
    used to have no index of its own (only idx_holdings_wallet(proxy_wallet,
    observed_at), a full covering-index read since observed_at isn't its
    leading column — dashboard review of c83964f); idx_holdings_observed
    (arb/whale_tape.py SCHEMA) now answers it directly. `None` for the tape
    probes when there is no tape (or it can't be opened ro) mirrors every
    other reader in this file: no tape is a fact, not an error."""
    prints_id = holdings_at = None
    con = report_whale.open_tape_ro(cfg)
    if con is not None:
        try:
            prints_id = con.execute("SELECT MAX(id) FROM prints").fetchone()[0]
            holdings_at = con.execute(
                "SELECT MAX(observed_at) FROM holdings").fetchone()[0]
        finally:
            con.close()
    bet_id = store.query("SELECT MAX(id) m FROM flat_bets")[0]["m"]
    return ("whale_bundle", prints_id, holdings_at, bet_id)


def bundle(cfg, store, now: datetime, ttl_secs: float, max_entries: int) -> dict:
    """{"state", "frame", "profiles", "now", "cached_at", "age_secs"} — the
    shared payload every whale-heavy dashboard endpoint reads from. `state`/
    `profiles` are None when there is no tape, matching what each endpoint
    already did solo.

    `now` in the returned payload is the `now` THIS bundle's `state`/`frame`
    were actually built from — the call's own `now` on a fresh compute, or
    the original compute's `now` on a cache hit, never the caller's current
    wall clock. `state` can be up to `ttl_secs` stale; a caller that then fed
    it a fresh `now` was internally inconsistent — e.g. `whale_exit.exit_reason`'s
    start gate closing (or opening) purely because wall-clock time passed
    between polls, not because anything on the tape changed (dashboard review
    of c83964f). Every endpoint deriving from `state`/`profiles` must use
    `b["now"]`, not a `now` it computed itself."""
    key = _probe_key(cfg, store)

    def compute():
        con = report_whale.open_tape_ro(cfg)
        try:
            state = score_state(cfg, con, now) if con is not None else None
            profiles = (whale_rank.profiles(state, cfg, now)
                       if state is not None else None)
            today = now.astimezone(ZoneInfo(str(cfg["timezone"]))).date().isoformat()
            frame = report_whale._frame_from_state(
                cfg, store, con, now, state, None, today)
        finally:
            if con is not None:
                con.close()
        return {"state": state, "frame": frame, "profiles": profiles, "now": now}

    return dashboard_cache.get_or_compute(key, ttl_secs, compute, max_entries=max_entries)
