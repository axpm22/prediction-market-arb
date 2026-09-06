"""The dashboard's L14 panel (Task 12): the same frame the daily report prints,
plus the live exit-rule status of each open WHALE bet.

Nothing is recomputed here. `report_whale.whale_frame` builds the section (so
the page and the report can never show two different "qualifying whales"), and
the exit status comes from `whale_exit.exit_reason` evaluated against the tape's
current state — the lane's own rule, run read-only rather than re-implemented.

The tape is opened `mode=ro`; a missing or unreadable tape is a fact the panel
reports, not an error it raises.
"""
from __future__ import annotations

from datetime import datetime, timezone

from arb import report_whale, whale_elite, whale_exit, whale_holdings, whale_mat
from arb.lanes.whale_book import _bet_ctx
from arb.whale_state import score_state


def whale_panel(cfg, store, now=None, con=None) -> dict:
    """`/api/whale`: the daily section's frame (which already carries the §12
    kill-rule block) as of now. `producing_day` is today (cfg timezone): the
    dashboard IS today's live view, the same "day being produced" cmd_report
    counts toward its own streak, so the two surfaces agree (§12 fix report 2)."""
    from zoneinfo import ZoneInfo
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(str(cfg["timezone"]))).date().isoformat()
    return report_whale.whale_frame(cfg, store, now=now, con=con, producing_day=today)


def traders_shape(cfg, now, level, top, min_n, wallet, profiles) -> dict:
    """The `/api/whale/traders` response, given an already-built `profiles`
    list (`whale_rank.profiles` over a state) — or `None` for no tape.

    Split out of `whale_traders` so the dashboard's shared bundle
    (arb/dashboard_whale_bundle.py) can build `profiles` ONCE per bundle and
    hand it here for every request, instead of `whale_traders` re-running
    `score_state` per request (the 2026-09-05 CPU-pileup fix)."""
    from arb import whale_rank
    rank_cfg = cfg["whale"]["rank"]
    level = level if level in whale_rank.DEPTH else "bucket"
    floor = int(rank_cfg["min_n"] if min_n is None else min_n)
    limit = int(rank_cfg["top"] if top is None else top)
    out = {"as_of": now.isoformat(timespec="seconds"), "tape_ok": profiles is not None,
           "level": level, "min_n": floor, "top": limit, "wallet": wallet,
           "rows": [], "best": []}
    if profiles is None:
        return out
    if wallet:
        out["rows"] = whale_rank.wallet_profile(profiles, wallet)
        out["best"] = whale_rank.best_markets(profiles, wallet, limit, floor)
    else:
        out["rows"] = whale_rank.rank(profiles, cfg, level=level, min_n=floor)[:limit]
    return out


def whale_traders(cfg, level="bucket", top=None, min_n=None, wallet=None,
                  now=None, con=None) -> dict:
    """`/api/whale/traders`: every scored wallet ranked (arb/whale_rank.py).

    Read-time diagnostics — nothing here gates a fire, and the lb80 a bucket
    row shows IS the one the fire rule reads. `wallet` swaps the table for that
    wallet's whole root -> sport -> bucket record plus its best buckets.

    Standalone entry point (tests, `arb whale traders`) — always builds its
    own `state`. The live dashboard instead reads `traders_shape` off the
    shared bundle; see that function's docstring.
    """
    from arb import whale_rank
    now = now or datetime.now(timezone.utc)
    opened = con is None and (con := report_whale.open_tape_ro(cfg)) is not None
    try:
        profiles = (whale_rank.profiles(score_state(cfg, con, now), cfg, now)
                   if con is not None else None)
        return traders_shape(cfg, now, level, top, min_n, wallet, profiles)
    finally:
        if opened:
            con.close()


def whale_positions(cfg, store, bets, now, con=None, state=None) -> dict[int, dict]:
    """{bet_id: whale block} for the open WHALE bets among `bets`.

    One scoring pass over the tape for all of them (`score_state`, the shared
    assembly), then the lane's own `exit_reason` per bet against the state the
    NEXT cycle would see. `exit_reason` returning None means the bet still
    passes all three rules — the position is held, and that is the status.

    `state` lets a caller that already ran `score_state` this refresh (the
    dashboard's shared bundle, arb/dashboard_whale_bundle.py) hand it in
    instead of a second tape scan; the default (None) preserves the old
    standalone behavior — build it here, from `con`.
    """
    whale_bets = [b for b in bets if b["kind"] == "WHALE"]
    if not whale_bets:
        return {}
    opened = con is None and (con := report_whale.open_tape_ro(cfg)) is not None
    if con is None:
        return {}                     # no tape: the panel says so, per bet
    try:
        state = state if state is not None else score_state(cfg, con, now)
        elite = whale_elite.index(cfg, state)
        pseudonym = {r["proxy_wallet"]: r["pseudonym"]
                     for r in con.execute("SELECT proxy_wallet, pseudonym FROM wallets")}
        out = {}
        for bet in whale_bets:
            ctx = _bet_ctx(con, bet)
            if ctx is None:
                continue              # booked without a signal row: nothing to say
            # the same elite bar the next cycle would use (§7 V5), off the tape
            # market's own bucket — without it a V5 bet would read as "held"
            # here whatever the consensus did.
            mk = state.markets.get(ctx["condition_id"], {})
            decision = whale_exit.exit_reason(
                ctx, state.open_positions, state.lookup, now, cfg,
                market_ctx={"elite": elite(mk.get("sport"), mk.get("market_type"))},
                # the same second `left` reading the lane runs (§9 item C,
                # 2026-09-04) — read-only, off the snapshots the lane polled,
                # so the panel cannot say "held" about a bet the next cycle
                # exits on the venue's own holdings. Same two corrections as
                # the lane (2026-09-06): the venue's own baseline since this
                # bet was booked, and the UNFILTERED positions for `left`.
                holdings=whale_holdings.for_bet(cfg, con, ctx, now,
                                                since=bet["created_at"]),
                holdings_base=whale_holdings.baseline_for_bet(
                    cfg, con, ctx, bet["created_at"]),
                all_positions=state.positions,
                cache_partial=whale_mat.cache_partial(con))
            out[bet["id"]] = {
                "signal_id": bet["signal_id"], "variant": ctx["variant"],
                "side": ctx["side"], "condition_id": ctx["condition_id"],
                "whales": [{"wallet": w["wallet"],
                            # pseudonym is the tape's own display name for a
                            # wallet; the address stays, it is a public fact
                            "pseudonym": pseudonym.get(w["wallet"]) or w["wallet"],
                            "lb": w.get("lb"), "native": w.get("native"),
                            "side": w.get("side"), "open_size": w.get("open_size"),
                            "sybil_dropped": bool(w.get("sybil_dropped"))}
                           for w in ctx["whales"]],
                "exit_reason": decision.reason if decision else None,
                "exit_detail": decision.detail if decision else {}}
        return out
    finally:
        if opened:
            con.close()
