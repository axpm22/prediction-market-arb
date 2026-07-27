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

    return {"rows": rows, "mix": dict(sorted(mix.items())),
            "from": from_date, "to": to_date,
            "archive_horizon": (date.today()
                                - timedelta(days=int(cfg["archive_after_days"]))).isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_secs": round(time.time() - t0, 1),
            "archives": [a.name for a in ARCHIVES]}


if __name__ == "__main__":
    from render_replay import render  # noqa: E402  (sibling module)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:2] == ["--render"]:  # re-render the last run; no DB access
        data = json.loads((OUT.parent / "data.json").read_text())
    else:
        a = sys.argv[1] if len(sys.argv) > 1 else "2026-07-20"
        b = sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat()
        data = main(a, b)
        (OUT.parent / "data.json").write_text(json.dumps(data, indent=1))
    OUT.write_text(render(data), encoding="utf-8")
    print(f"\nwrote {OUT} ({OUT.stat().st_size // 1024} KB) in {data['total_secs']}s")
