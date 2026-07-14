"""Order tickets — READ-ONLY (rule 1 untouched, no order code exists here).

Turns open locked flags into explicit human instructions (venue, market id,
the venue's OWN side label, price, depth cap) ranked by PERSISTENCE: how many
consecutive polls the position was re-sighted (1 booking + N position-absorb
annotations). Persistence at 300s observation latency is the number that
decides whether a flag is executable at all; a flag seen once is a ghost.
Any actual execution is the operator's manual act, outside this codebase,
behind the §18.2 gate."""
from __future__ import annotations

import json

from arb.models import LOCKED_KINDS


def open_locked_positions(cfg, store) -> list[dict]:
    ks = ",".join("?" * len(LOCKED_KINDS))
    bets = store.query(
        f"""SELECT fb.* FROM flat_bets fb
            WHERE fb.kind IN ({ks})
              AND fb.id = (SELECT MAX(f2.id) FROM flat_bets f2
                           WHERE f2.position_key = fb.position_key)
              AND NOT EXISTS (SELECT 1 FROM bet_events be
                              WHERE be.bet_id = fb.id
                                AND be.kind = 'lifetime_closed')""",
        tuple(LOCKED_KINDS))
    out = []
    for b in bets:
        # persistence = every poll that flagged this position (opportunities
        # rows land each poll; absorbs only fire on changed fingerprints)
        seen = store.query(
            """SELECT COUNT(*) c, MAX(p.started_at) last
               FROM opportunities o JOIN polls p ON p.id = o.poll_id
               WHERE o.position_key = ?""", (b["position_key"],))[0]
        out.append({
            "position_key": b["position_key"], "kind": b["kind"],
            "lane": b["lane"], "trusted": bool(b["trusted"]),
            "event_key": b["event_key"], "edge_points": b["edge_points"],
            "first_seen": b["created_at"],
            "last_seen": seen["last"] or b["created_at"],
            "sightings": max(1, seen["c"]),
            "legs": json.loads(b["legs_json"]),
        })
    out.sort(key=lambda t: (-t["sightings"], t["position_key"]))
    return out
