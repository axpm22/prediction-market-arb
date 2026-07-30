"""Cover-survival study over a time window (read-only).

The question a 24h run has to answer is not "did a cover appear" — the
opportunities table already says that — but "would any of them have PAID".
A cover only pays if all four hold at once:

  1. the legs sum to < 1 after fees                    (edge_net > 0)
  2. you can cross the spread and keep the edge        (spread <= edge)
  3. there is size at the touch                        (depth >= floor)
  4. it lives long enough to hit both legs             (lifetime >= fill window)

Every one of those has killed the lane at some point, so this reports them as
a funnel rather than a single number: each stage shows what survived the ones
before it. Anything that reaches the last row is money the model left on the
table; an empty last row over a full day is a decisive negative.

Read-only, no venue calls, no credits. Lifetime is a CENSORED lower bound —
a cover that opens and closes between two polls is invisible at any cadence.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _legs(row) -> list[dict]:
    try:
        return json.loads(row["legs_json"]) or []
    except Exception:  # noqa: BLE001 — a malformed row is data, not a crash
        return []


def _spread(legs) -> float | None:
    """Half-spread summed across legs — what crossing costs. None if any leg
    has no bid, which is itself disqualifying: a book you cannot exit."""
    total = 0.0
    for q in legs:
        bid, cost = _f(q.get("bid")), _f(q.get("cost"))
        if bid is None or cost is None:
            return None
        total += (cost - bid) / 2
    return total


def _depth(legs) -> float | None:
    ds = [_f(q.get("depth_usd")) for q in legs]
    return None if any(d is None for d in ds) else min(ds)


def collect(con, since: str, kinds=("XSPREAD",)) -> dict:
    """Funnel counts plus the survivors, over polls started at/after `since`."""
    marks = ",".join("?" * len(kinds))
    rows = [dict(r) for r in con.execute(
        f"""SELECT o.fingerprint, o.kind, o.event_key, o.edge, o.status,
                   o.legs_json, p.started_at
            FROM opportunities o JOIN polls p ON p.id = o.poll_id
            WHERE o.kind IN ({marks}) AND p.started_at >= ?
            ORDER BY p.started_at""", (*kinds, since))]
    seen: dict[str, dict] = {}
    for r in rows:
        f = seen.setdefault(r["fingerprint"], {
            "first": r["started_at"], "last": r["started_at"], "hits": 0,
            "event_key": r["event_key"], "edge": r["edge"],
            "legs": _legs(r), "status": r["status"]})
        f["last"] = r["started_at"]
        f["hits"] += 1
    return {"observations": len(rows), "distinct": len(seen), "covers": seen}


def funnel(cfg, con, since: str) -> dict:
    """The four gates, applied in order, each on what survived the last."""
    got = collect(con, since)
    covers = got["covers"]
    min_depth = float(cfg["min_executable_usd"])
    stages = {"detected": [], "edge_net>0": [], "spread<=edge": [],
              f"depth>=${min_depth:.0f}": [], "survived>=2 polls": []}
    for fp, c in covers.items():
        edge = _f(c["edge"], 0.0) or 0.0
        stages["detected"].append(fp)
        if edge <= 0:
            continue
        stages["edge_net>0"].append(fp)
        sp = _spread(c["legs"])
        if sp is None or sp > edge:
            continue
        stages["spread<=edge"].append(fp)
        dp = _depth(c["legs"])
        if dp is None or dp < min_depth:
            continue
        stages[f"depth>=${min_depth:.0f}"].append(fp)
        if c["hits"] < 2:
            continue
        stages["survived>=2 polls"].append(fp)
    return {"since": since, "observations": got["observations"],
            "stages": {k: len(v) for k, v in stages.items()},
            "survivors": [covers[f] for f in stages["survived>=2 polls"]],
            "min_depth_usd": min_depth}


def render(res: dict) -> str:
    out = [f"cover-survival funnel since {res['since']}",
           f"  {res['observations']} cover observations "
           f"-> {res['stages'].get('detected', 0)} distinct covers", ""]
    prev = None
    for name, n in res["stages"].items():
        lost = "" if prev is None else f"   (-{prev - n})"
        out.append(f"    {name:24} {n:>6}{lost}")
        prev = n
    out.append("")
    if not res["survivors"]:
        out.append("  NOTHING survived every gate — no cover in this window was")
        out.append("  executable. Lifetime is censored, so this is an upper bound")
        out.append("  on what a faster loop could have caught, never a lower one.")
    else:
        out.append(f"  {len(res['survivors'])} executable cover(s):")
        for c in res["survivors"][:20]:
            life = ""
            try:
                life = (f"  lifetime>={(datetime.fromisoformat(c['last']) - datetime.fromisoformat(c['first'])).total_seconds():.0f}s")
            except Exception:  # noqa: BLE001
                pass
            out.append(f"    edge={c['edge']} hits={c['hits']}{life}  "
                       f"{str(c['event_key'])[:52]}")
    return "\n".join(out)
