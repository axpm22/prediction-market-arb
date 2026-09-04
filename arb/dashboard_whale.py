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

from arb import report_whale, whale_exit
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


def whale_positions(cfg, store, bets, now, con=None) -> dict[int, dict]:
    """{bet_id: whale block} for the open WHALE bets among `bets`.

    One scoring pass over the tape for all of them (`score_state`, the shared
    assembly), then the lane's own `exit_reason` per bet against the state the
    NEXT cycle would see. `exit_reason` returning None means the bet still
    passes all three rules — the position is held, and that is the status.
    """
    whale_bets = [b for b in bets if b["kind"] == "WHALE"]
    if not whale_bets:
        return {}
    opened = con is None and (con := report_whale.open_tape_ro(cfg)) is not None
    if con is None:
        return {}                     # no tape: the panel says so, per bet
    try:
        state = score_state(cfg, con, now)
        pseudonym = {r["proxy_wallet"]: r["pseudonym"]
                     for r in con.execute("SELECT proxy_wallet, pseudonym FROM wallets")}
        out = {}
        for bet in whale_bets:
            ctx = _bet_ctx(con, bet)
            if ctx is None:
                continue              # booked without a signal row: nothing to say
            decision = whale_exit.exit_reason(ctx, state.open_positions,
                                              state.lookup, now, cfg)
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
