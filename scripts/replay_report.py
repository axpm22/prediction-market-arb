"""Build the local replay panel: per-day/per-kind `arb replay` over the hot DB,
plus the venue quote mix either side of the PM/ANJ outage start (2026-07-16).

Read-only throughout — the hot DB and the monthly archive are both opened
`mode=ro`. Writes exactly one file, reports/replay/index.html (gitignored).

Usage: .venv/bin/python scripts/replay_report.py [FROM] [TO]
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arb.config import load                       # noqa: E402
from arb.replay import open_ro, replay            # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "replay" / "index.html"
HOT = ROOT / "data" / "arb.sqlite"
ARCHIVES = sorted((ROOT / "data" / "archive").glob("arb-*.sqlite"))


def days(a: str, b: str):
    d, end = date.fromisoformat(a), date.fromisoformat(b)
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def venue_mix(con, where: str, params=()) -> dict:
    """{utc_day: {venue: n_quotes}} — a plain aggregate, NOT a replay."""
    out: dict = {}
    for r in con.execute(f"SELECT substr(fetched_at,1,10) d, venue, COUNT(*) n "
                         f"FROM quotes WHERE {where} GROUP BY d, venue", params):
        out.setdefault(r[0], {})[r[1]] = r[2]
    return out


class ReadOnlyStore:
    """The `.query` surface metrics.py/report.py use, over a mode=ro handle.
    They only ever SELECT; this makes that structural rather than a promise."""

    def __init__(self, path):
        self.conn = open_ro(path)

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()


def pnl() -> dict:
    """Paper P&L straight out of the ledger — NOT from replay. Replay is
    detection-only; P&L needs flat_bets + bet_events, which it never touches."""
    from collections import defaultdict
    from decimal import Decimal

    from arb import metrics, report

    cfg = load(ROOT / "config.yaml")
    st = ReadOnlyStore(HOT)
    head_total, head_n = report.headline(cfg, st)
    rows = [dict(r) for r in st.query("SELECT * FROM flat_bets ORDER BY id")]
    settled = {r[0] for r in st.query(
        "SELECT DISTINCT bet_id FROM bet_events WHERE kind = 'settled'")}

    agg: dict = defaultdict(lambda: {"n": 0, "stake": Decimal(0), "opt": Decimal(0),
                                     "conf": Decimal(0), "missed": 0, "open": 0,
                                     "excluded": 0})
    for r in rows:
        a = agg[(r["lane"], r["kind"])]
        a["n"] += 1
        a["stake"] += Decimal(str(r["stake_usd"]))
        if r["id"] not in settled:
            a["open"] += 1
            continue
        if metrics._annotated(st, r["id"]):
            a["excluded"] += 1        # invariant 5: never enters a P&L cell
            continue
        opt = metrics.bet_pnl_optimistic(st, r)
        if opt is not None:
            a["opt"] += opt
        confirmed, c = metrics.bet_confirmed(st, r)
        if not confirmed:
            a["missed"] += 1          # never actually filled -> $0, and counted
        elif c is not None:
            a["conf"] += c
    return {"headline": str(head_total), "headline_n": head_n,
            "rows": [{"lane": lane, "kind": kind, **{k: (str(v) if isinstance(v, Decimal)
                                                     else v) for k, v in a.items()}}
                     for (lane, kind), a in sorted(agg.items())],
            "first_bet": rows[0]["created_at"][:10] if rows else None,
            "last_bet": rows[-1]["created_at"][:10] if rows else None}


def main(from_date: str, to_date: str) -> dict:
    cfg = load(ROOT / "config.yaml")
    con = open_ro(HOT)
    rows, t0 = [], time.time()
    for day in days(from_date, to_date):
        for kind in ("watch", "live"):
            t = time.time()
            s = replay(cfg, con, day, day, kind)
            rows.append({"day": day, "kind": kind, "polls": s.polls,
                         "quotes": s.quotes, "secs": round(time.time() - t, 1),
                         "edge_total": str(s.edge_total),
                         "opps": {f"{k}/{v}": n for (k, v), n in s.opps.items()},
                         "rejects": dict(s.rejects), "both": s.both,
                         "replay_only": s.replay_only, "stored_only": s.stored_only})
            print(f"{day} {kind:5} polls={s.polls:5} quotes={s.quotes:8} "
                  f"diff={s.both}/{s.replay_only}/{s.stored_only} "
                  f"({rows[-1]['secs']}s)", flush=True)

    mix = venue_mix(con, "fetched_at >= ?", (from_date,))
    for a in ARCHIVES:  # one full scan each; the archive carries no indexes
        t = time.time()
        arch = sqlite3.connect(f"file:{a}?mode=ro", uri=True)
        mix.update(venue_mix(arch, "1=1"))
        arch.close()
        print(f"archive {a.name}: venue mix scanned in {time.time() - t:.1f}s", flush=True)

    return {"rows": rows, "mix": dict(sorted(mix.items())), "pnl": pnl(),
            "from": from_date, "to": to_date,
            "archive_horizon": (date.today()
                                - timedelta(days=int(cfg["archive_after_days"]))).isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_secs": round(time.time() - t0, 1),
            "archives": [a.name for a in ARCHIVES]}


if __name__ == "__main__":
    from render_replay import render  # noqa: E402  (sibling module)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:2] == ["--render"]:  # reuse cached replay rows; P&L is cheap
        data = json.loads((OUT.parent / "data.json").read_text())
        data["pnl"] = pnl()
        (OUT.parent / "data.json").write_text(json.dumps(data, indent=1))
    else:
        a = sys.argv[1] if len(sys.argv) > 1 else "2026-07-20"
        b = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
        data = main(a, b)
        (OUT.parent / "data.json").write_text(json.dumps(data, indent=1))
    OUT.write_text(render(data), encoding="utf-8")
    print(f"\nwrote {OUT} ({OUT.stat().st_size // 1024} KB) in {data['total_secs']}s")
