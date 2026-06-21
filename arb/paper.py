from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from .models import Opportunity
from .pricing import money
from .store import Store, quote_to_json


def fill_opportunity(store: Store, scan_id: int, opportunity: Opportunity, bankroll: Decimal, max_stake_pct: Decimal) -> int | None:
    # SPEC §1/§9: DIVERGENCE is logged with stake 0 and never bet; REVIEW (e.g. cross-venue
    # pairs not whitelisted in trusted_pairs) is displayed but never auto-filled.
    if opportunity.kind == "DIVERGENCE" or opportunity.status != "OK":
        return None
    budget = money(bankroll * max_stake_pct)
    guaranteed_payout = None
    if opportunity.kind == "ARB":
        guaranteed_payout = money(budget * (Decimal("1") + opportunity.edge))
    with store.connect() as con:
        cur = con.execute(
            """
            INSERT INTO positions(opened_scan_id, event_key, kind, legs_json, stakes_json, cost_usd,
                                  guaranteed_payout_usd, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (
                scan_id,
                opportunity.event.event_key,
                opportunity.kind,
                json.dumps([quote_to_json(leg) for leg in opportunity.legs]),
                json.dumps({key: str(value) for key, value in opportunity.stakes.items()}),
                str(budget),
                None if guaranteed_payout is None else str(guaranteed_payout),
            ),
        )
        position_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO ledger(ts, type, position_id, amount_usd, balance_after) VALUES (?, 'FILL', ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                position_id,
                str(-budget),
                str(money(bankroll - budget)),
            ),
        )
    return position_id



