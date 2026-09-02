"""Score the maker bracket (L12) SEPARATELY for each day of tape.

The point of recording more days was never n. It was this: KNOWN_ISSUES #12
records a +0.0429 pooled edge that split into +0.0681 early / +0.0172
[-0.0159, +0.0528] late and failed to confirm, and the ledger's verdict is
that the temporal split is "load-bearing, not ceremony". A maker cost measured
on a single day cannot be split, so it cannot be confirmed — only asserted.

Read-only: the tape is opened mode=ro and a per-day VIEW is substituted for
tape_frames, so arb/bench_maker.replay runs unmodified against one day at a
time.

Usage: PYTHONPATH=. .venv/bin/python scripts/maker_day_split.py
"""
from __future__ import annotations

import sqlite3
import sys

from arb import bench_maker
from arb.config import load


def days(tape_path: str) -> list[str]:
    con = sqlite3.connect(f"file:{tape_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT date(received_at,'unixepoch') d, count(*) n FROM tape_frames "
        "GROUP BY d ORDER BY d").fetchall()
    con.close()
    return [(d, n) for d, n in rows]


def scored(cfg, tape_path: str, day: str) -> dict:
    con = sqlite3.connect(":memory:", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("ATTACH DATABASE ? AS t", (f"file:{tape_path}?mode=ro",))
    # A day string comes from date() on our own column, never from user input.
    con.execute(
        "CREATE TEMP VIEW tape_frames AS SELECT * FROM t.tape_frames "
        f"WHERE date(received_at,'unixepoch') = '{day}'")
    try:
        return bench_maker.replay(cfg, con)
    finally:
        con.close()


def main() -> int:
    cfg = load("config.yaml")
    tape = str(cfg["maker"]["tape_db"])
    print(f"{'day':<12}{'frames':>10}{'prints':>8}{'fills':>7}"
          f"{'n':>6}{'mean':>10}{'95% CI':>22}")
    print("-" * 75)
    means = []
    for day, nframes in days(tape):
        res = scored(cfg, tape, day)
        b = res["bracket"]["conservative"]
        n = b["n"]
        if n:
            means.append((day, float(b["mean"]), n))
        ci = f"[{b['lo']}, {b['hi']}]" if n else "-"
        print(f"{day:<12}{nframes:>10,}{res['prints']:>8}{res['fills']:>7}"
              f"{n:>6}{b['mean'] if n else '-':>10}{ci:>22}")
    print()
    if len(means) >= 2:
        lo = min(m for _, m, _ in means)
        hi = max(m for _, m, _ in means)
        print(f"spread across days: {lo:.4f} .. {hi:.4f}")
        print("all days negative: "
              f"{'YES' if hi < 0 else 'NO — the sign is not stable'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
