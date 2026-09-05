"""The L14 whale section as markdown — the render half of arb/report_whale.py.

Pure formatting over the frame that module builds: nothing here reads the
ledger, the tape or the clock, so the section a past day renders is a function
of that day's frame alone (rule 9's re-run determinism).

Split out 2026-09-04 (non-sports roots fix pass) on size alone: every §12
table gained a per-root block, and `report_whale.py` was already past its
~300-line budget.
"""
from __future__ import annotations

from decimal import Decimal

from arb import whale_rank
from arb.whale_metrics import roots_first


def _n(v, dash="—") -> str:
    return dash if v is None else str(v)


def _mb(b) -> str:
    return "—" if b is None else f"{Decimal(b) / 1000000:.1f}"


def _elite_line(elite: dict) -> str:
    """V5's bar per bucket, with the population behind it. A cutoff at or below
    `min_score_lb` selects EVERY qualifying whale — V5 is then a copy of V1,
    which is a result about the config, not about the whales, and is said here
    as well as logged by the cycle."""
    bar = Decimal(str(elite["min_score_lb"]))
    cells = []
    for r in elite["cutoffs"]:
        cut = r["cutoff"]
        cells.append(f"{r['bucket']} {_n(cut)} (pop {r['n']})"
                     + ("" if cut is None or Decimal(cut) > bar
                        else f" **<= min_score_lb {bar}: V5 is a copy of V1**"))
    return (f"- V5 elite bar (top {elite['pct']}% of every scored wallet in the "
            f"root): " + (", ".join(cells) or "—"))


def _root_md(frame: dict, root: str) -> list[str]:
    """One root's three §12 tables — variants, markouts, exits. Its own block
    because the registration forbids pooling roots: a reader must never be able
    to add two cells together by accident."""
    bets, roi = frame["bets"].get(root, {}), frame["roi"].get(root, {})
    md = ["", f"### {root} root"
          + (" — PRIMARY" if root == frame["primary_root"] else " (never the headline)"),
          "",
          "| variant | bets | settled | events | ROI/$ | lb80 | ub90 | win rate |",
          "|---|---|---|---|---|---|---|---|"]
    for variant in sorted(set(bets) | set(roi)):
        r = roi.get(variant)
        star = " **(primary)**" if (variant == frame["primary_variant"]
                                    and root == frame["primary_root"]) else ""
        md.append(f"| {variant}{star} | {bets.get(variant, 0)} | "
                  + (f"{r['n']} | {r['n_events']} | {r['mean']} | {r['lb80']} | "
                     f"{r['ub90']} | {r['win_rate']} |" if r else "0 | 0 | — | — | — | — |"))
    if not bets and not roi:
        md.append("| — | 0 | 0 | 0 | — | — | — | — |")
    md += ["", "| markout (pregame) | coverage | marks | mean drift |", "|---|---|---|---|"]
    marks = frame["markouts"].get(root, {})
    for h, cell in sorted(marks.items()):
        md.append(f"| +{h // 60}m | {cell['coverage']} | {cell['n']} | "
                  f"{_n(cell['mean_drift'])} |")
    if not marks:
        md.append("| — | 0 | 0 | — |")
    md += ["", "| exit reason | exits | exit P&L | hold P&L (counterfactual) | resolved |",
           "|---|---|---|---|---|"]
    exits = frame["exits"].get(root, {})
    for reason, cell in sorted(exits.items()):
        md.append(f"| {reason} | {cell['n']} | ${cell['exit_pnl']} | "
                  f"${cell['hold_pnl']} | {cell['n_hold']} |")
    if not exits:
        md.append("| — | 0 | $0 | $0 | 0 |")
    return md


def whale_md(frame: dict) -> list[str]:
    """The section, as markdown. Cumulative lane-to-date except where a line
    names its own window — the class table above it is the day's."""
    w, s, c = frame["wallets"], frame["signals"], frame["collector"]
    cal, q = frame["calibration"], frame["qualifying"]
    md = ["## L14 whale-consensus lane (REVIEW lane, never headline)",
          f"- as of {frame['as_of']}"
          + ("" if frame["tape_ok"] else "  — **whale tape unavailable**: "
                                         "collector never ran here, or the file "
                                         "could not be opened read-only"),
          f"- wallets: {w['tracked']} tracked, {w['backfilled']} backfilled, "
          f"{w['hygiene_excluded']} positions hygiene-excluded",
          f"- whales scored >= lb80 (ungated roster): {q['n']} "
          + (", ".join(f"{k} {v}" for k, v in sorted(q["by_root"].items())) or "—")
          + " | by sport: "
          + (", ".join(f"{k} {v}" for k, v in sorted(q["by_sport"].items())) or "—"),
          f"- signals 24h: {s['evaluated_24h']} evaluated, {s['fired_24h']} fired "
          f"(distinct signal_id booked); 7d fired {s['fired_7d']} — ALL roots; "
          f"kill rule 4's {frame['kill']['min_signals_per_week_alarm']}/week "
          f"starvation floor counts the {frame['primary_root']} root only",
          "- suppressed 24h: " + (", ".join(
              f"{reason or '—'}/{variant} {n}"
              for (reason, variant), n in sorted(s["suppressed"].items(),
                                                 key=lambda kv: -kv[1])) or "none"),
          f"- calibration: min_points "
          + " ".join(f"{k}={_n(v, 'null')}" for k, v in sorted(cal["min_points"].items()))
          + f"; {cal['signal_days']} of {cal['calibration_days']} signal days",
          "- base rate: " + (", ".join(
              f"{r['root']} {r['value']} (n={r['n']})" for r in frame["base_rates"])
              or "—"),
          _elite_line(frame["elite"]),
          # last cycle status/rows/bad_rows/at and backoff_until are LIVE
          # collector facts, not a function of the report day — the
          # dashboard shows them (frame["collector"] still carries them);
          # this hashed line only prints what a re-run of a past day must
          # always reproduce (rule 9).
          f"- collector: newest print {_n(c['last_seen_age_secs'])}s ago; "
          f"{c['prints_per_min']} prints/min (1h)"
          + (f"; **gap** {c['last_gap'].get('secs')}s at {c['last_gap'].get('at')}"
             if c["last_gap"] else "; no recorded gap"),
          # the file size and MB/day are live (see _collector): whale_line
          f"- tape: {c['prints_24h']} prints/day, {c['holdings_24h']} holdings "
          "rows/day (24h to as-of)",
          "",
          "| kill rule (§12) | state | numbers |", "|---|---|---|"]
    for r in frame["kill"]["rules"]:
        md.append(f"| {r['rule']} {r['name']} | "
                  + (f"**{r['state']}**" if r["state"] == "FIRED" else r["state"])
                  + f" | {r['says']} |")
    kill_roots = frame["kill"].get("roots") or {}
    md += ["",
           f"Primary: mean settled ROI per $, {frame['primary_variant']}, "
           f"{frame['primary_root']} root, clustered by event_key. Bounds are "
           f"ONE-SIDED (§12): lb80 = 20th percentile, ub90 = 90th. Decision at "
           f"n >= {frame['kill']['decision_n']} signals; upper 90% bound < 0 "
           f"kills the lane. Roots are NEVER pooled (registration 2026-09-04): "
           f"each block below is its own population.",
           "- non-sports roots (own n, never the primary decision): " + (", ".join(
               f"{root} n={c['n']} ub90 {_n(c['ub90'])} streak {c['streak']}"
               for root, c in sorted(kill_roots.items())) or "none")]
    for root in (roots_first(frame["bets"], frame["roi"], frame["markouts"],
                             frame["exits"]) or [frame["primary_root"]]):
        md += _root_md(frame, root)
    md += ["", f"| whales on the fired side ({frame['primary_root']}) | "
           "signals booked | ROI/$ | lb80 | ub90 |",
           "|---|---|---|---|---|"]
    for k in ("native", "transfer"):
        r = frame["native"]["roi"].get(k)
        md.append(f"| {k} | {frame['native']['signals'][k]} | "
                  + (f"{r['mean']} | {r['lb80']} | {r['ub90']} |" if r else "— | — | — |"))
    if q["top"]:
        md += ["", "| whale | bucket | lb80 | n_bucket | evidence |", "|---|---|---|---|---|"]
        for row in q["top"]:
            md.append(f"| {row['wallet']} | {row['bucket']} | {row['lb']} | "
                      f"{row['n_bucket']} | "
                      f"{'native' if row['native'] else 'transfer'} |")
    return md + whale_rank.traders_md(frame["traders"])


def whale_line(frame: dict) -> str:
    """One console line for `arb report --daily` (the md carries the section)."""
    primary = frame["roi"].get(frame["primary_root"], {}).get(
        frame["primary_variant"])
    roi = (f"{primary['mean']} [lb80 {primary['lb80']}, ub90 {primary['ub90']}] "
           f"n={primary['n']}/{primary['n_events']} events" if primary else "no sample")
    c = frame["collector"]
    mat = c.get("mat") or {}
    return (f"L14 whale: {frame['signals']['evaluated_24h']} signals/24h, "
            f"{frame['signals']['fired_24h']} fired; {frame['primary_variant']} "
            f"ROI/$ {roi}; collector {_n(c['status'])}; tape {_mb(c['tape_bytes'])} MB, "
            f"~{_mb(c['bytes_per_day'])} MB/day"
            # matpos (2026-09-05): the three numbers that say whether the
            # scoring cache is keeping up — how big it is, how many wallets
            # are still queued for recompute, and what the last pass cost.
            # A backlog that does not fall cycle to cycle is the alarm.
            + (f"; positions {mat['rows']}, {mat['dirty']} dirty, "
               f"{mat['secs']}s" if mat else "")
            # ... and, when the lane is not keeping that cache current at all,
            # what every number above was computed off (I2, 2026-09-05).
            + (f"; {c['mat_stale']}" if c.get("mat_stale") else "")
            + ("" if frame["tape_ok"] else "; TAPE UNAVAILABLE"))
