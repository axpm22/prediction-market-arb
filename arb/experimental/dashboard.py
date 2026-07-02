"""Read-only dashboard data — BUILD_PLAN Phase 6.

Queries the ledger and writes reports/dashboard.json (equity curves, segment scores,
grade counts, clean-vs-raw EV). A static reports/dashboard.html (Chart.js from CDN) renders
it. NEVER places trades; pure read. Safe to publish (no secrets in the JSON).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.scoring import score_segments
from arb.store import Store

# The ledger kinds the per-type section always surfaces (each separated; 0-row kinds shown empty).
CANONICAL_KINDS = ("ARB", "EV", "DIVERGENCE", "CLOSE", "NEGRISK", "LIVE_BASEBALL_EV")


def build_dashboard_data(store: Store, config) -> dict:
    from .annotations import annotated_ids
    with store.connect() as con:
        # PURGE_LOOP_PNL: annotated rows (bug:settled-rebook-loop) are excluded from EVERY cell of
        # the honest-variant matrix — equity/kelly curves, drawdowns, per_type, edge buckets all
        # derive from `settled`, so filtering it here purges loop P&L from all of them at once.
        excluded = annotated_ids(con)
        settled = [
            row for row in con.execute(
                """SELECT id, placed_at, pnl_optimistic, pnl_confirmed,
                          pnl_kelly_optimistic, pnl_kelly_confirmed,
                          kind, quality_flag, edge, stake_usd, result
                     FROM flat_bets WHERE status='SETTLED' AND result IN ('WON','LOST','VOID','RESOLVED')
                    ORDER BY placed_at"""
            )
            if int(row["id"]) not in excluded
        ]
        grade_rows = list(
            con.execute("SELECT status, COUNT(*) n FROM flat_bets WHERE kind='ARB' GROUP BY status")
        )
        arb_open = list(
            con.execute("SELECT COUNT(*) n, COALESCE(SUM(CAST(edge AS REAL)),0) e FROM flat_bets WHERE kind='ARB' AND status='OPEN'")
        )[0]

    equity = {
        "optimistic_raw": _curve(settled, "pnl_optimistic", clean_only=False),
        "optimistic_clean": _curve(settled, "pnl_optimistic", clean_only=True),
        "confirmed_raw": _curve(settled, "pnl_confirmed", clean_only=False),
        "confirmed_clean": _curve(settled, "pnl_confirmed", clean_only=True),
    }
    kelly_equity = {
        "optimistic_raw": _curve(settled, "pnl_kelly_optimistic", clean_only=False),
        "optimistic_clean": _curve(settled, "pnl_kelly_optimistic", clean_only=True),
        "confirmed_raw": _curve(settled, "pnl_kelly_confirmed", clean_only=False),
        "confirmed_clean": _curve(settled, "pnl_kelly_confirmed", clean_only=True),
    }
    scores = score_segments(store, config.scoring)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # The hero number — the ONLY figure that aggregates across bet types. Everything in
        # per_type below is kept separate. confirmed/clean/flat-$1 is the project's honest baseline.
        "headline": {
            "total_profit": round(_final(equity["confirmed_clean"]), 4),
            "basis": "confirmed / clean / flat-$1",
            "note": "only number aggregated across kinds; per_type is never blended",
        },
        # Per-bet-type performance: one line per kind, % and $, confirmed/clean by default, with
        # head-to-head version columns (re-grades of the SAME bets under a different rule — never
        # presented as independent bets). Each type is its own row, never folded into a sibling.
        "per_type": _per_type(settled),
        "equity": equity,
        "kelly_equity": kelly_equity,
        "drawdowns": {name: _drawdown(points) for name, points in equity.items()},
        "kelly_drawdowns": {name: _drawdown(points) for name, points in kelly_equity.items()},
        "optimistic_confirmed_gap": {
            "raw": round(_final(equity["optimistic_raw"]) - _final(equity["confirmed_raw"]), 4),
            "clean": round(_final(equity["optimistic_clean"]) - _final(equity["confirmed_clean"]), 4),
        },
        "ev_edge_buckets": _ev_edge_buckets(settled),
        "left_on_table": {
            "persisted": False,
            "note": "watch prints filtered flags, but no append-only table stores them yet",
        },
        "copy_trade_clv": _copy_trade_clv(store, config),
        "arb_grades": {r["status"]: r["n"] for r in grade_rows},
        "arb_open": {"n": arb_open["n"], "sum_edge": round(float(arb_open["e"]), 4)},
        "segment_scores": [
            {"segment": s.segment, "n": s.n, "win_rate": s.win_rate, "roi": s.roi,
             "score": s.score, "tier": s.tier}
            for s in scores
        ],
    }


def write_dashboard(store: Store, config, out_dir: Path = Path("reports")) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = build_dashboard_data(store, config)
    json_path = out_dir / "dashboard.json"
    json_path.write_text(json.dumps(data, indent=1))
    html_path = out_dir / "dashboard.html"
    html_path.write_text(_HTML)
    return json_path


def _per_type(rows: list) -> list[dict]:
    """One performance line per ledger kind. confirmed/clean/flat-$1 is the default headline
    metric; `versions` holds re-grades of the SAME bets (optimistic, raw, Kelly) for A/B."""
    by_kind: dict[str, list] = defaultdict(list)
    for row in rows:
        by_kind[row["kind"]].append(row)
    kinds = list(CANONICAL_KINDS) + [k for k in sorted(by_kind) if k and k not in CANONICAL_KINDS]
    seen: set[str] = set()
    out = []
    for kind in kinds:
        if kind in seen:
            continue
        seen.add(kind)
        out.append(_type_stats(kind, by_kind.get(kind, [])))
    return out


def _type_stats(kind: str, rows: list) -> dict:
    clean = [r for r in rows if r["quality_flag"] is None]
    decided = [r for r in clean if r["result"] in ("WON", "LOST")]
    wins = sum(1 for r in decided if r["result"] == "WON")
    # win_rate is only defined where bets decide WON/LOST; arbs settle RESOLVED (a held lock),
    # so their win_rate is None rather than a misleading 0.
    win_rate = round(wins / len(decided), 4) if decided else None
    default = _variant(rows, "pnl_confirmed", clean_only=True)
    return {
        "kind": kind,
        "n": len(clean),
        "win_rate": win_rate,
        "pnl": default["pnl"],            # confirmed/clean/flat-$1 (the honest default)
        "staked": default["staked"],
        "roi": default["roi"],
        "sparkline": _variant_curve(rows, "pnl_confirmed", clean_only=True),
        "status": "measurement-only",     # paper-only; no order path exists
        "versions": {
            "confirmed_clean_flat": default,
            "optimistic_clean_flat": _variant(rows, "pnl_optimistic", clean_only=True),
            "confirmed_raw_flat": _variant(rows, "pnl_confirmed", clean_only=False),
            "confirmed_clean_kelly": _variant(rows, "pnl_kelly_confirmed", clean_only=True),
        },
        "versions_are_regrades": True,    # same bets, different grading — NOT independent evidence
        # optimistic minus confirmed = the "mirage": locked-at-entry vs actually-confirmed P&L.
        "mirage_gap": round(_variant(rows, "pnl_optimistic", clean_only=True)["pnl"] - default["pnl"], 4),
    }


def _variant(rows: list, field: str, clean_only: bool) -> dict:
    """$ / % for one grading variant (a field × clean/raw choice). ROI = P&L / total staked."""
    pnl = 0.0
    staked = 0.0
    n = 0
    for row in rows:
        if clean_only and row["quality_flag"] is not None:
            continue
        value = row[field]
        if value is None:
            continue
        pnl += float(value)
        staked += float(row["stake_usd"] or 0)
        n += 1
    return {"pnl": round(pnl, 4), "staked": round(staked, 2),
            "roi": round(pnl / staked, 4) if staked else 0.0, "n": n}


def _variant_curve(rows: list, field: str, clean_only: bool) -> list[float]:
    """Cumulative P&L sparkline (values only) for one type's variant."""
    cum = 0.0
    out: list[float] = []
    for row in rows:
        if clean_only and row["quality_flag"] is not None:
            continue
        value = row[field]
        if value is None:
            continue
        cum += float(value)
        out.append(round(cum, 4))
    return out


def _curve(rows: list, field: str, clean_only: bool) -> list[dict]:
    cum = 0.0
    out = []
    for row in rows:
        if clean_only and row["quality_flag"] is not None:
            continue
        value = row[field]
        if value is None:
            continue
        cum += float(value)
        out.append({"t": row["placed_at"], "cum": round(cum, 4)})
    return out


def _drawdown(points: list[dict]) -> dict[str, float]:
    peak = 0.0
    worst = 0.0
    for point in points:
        cum = float(point["cum"])
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return {"max_drawdown": round(worst, 4), "max_drawdown_abs": round(abs(worst), 4)}


def _final(points: list[dict]) -> float:
    return float(points[-1]["cum"]) if points else 0.0


def _ev_edge_buckets(rows: list) -> dict[str, dict[str, dict[str, float | int]]]:
    buckets = {
        "0-1%": _bucket(),
        "1-2%": _bucket(),
        "2-5%": _bucket(),
        ">5%": _bucket(),
    }
    for row in rows:
        if row["kind"] != "EV":
            continue
        bucket = _bucket_name(Decimal(str(row["edge"])))
        _add_bucket(buckets[bucket]["raw"], row["pnl_optimistic"])
        if row["quality_flag"] is None:
            _add_bucket(buckets[bucket]["clean"], row["pnl_optimistic"])
    return buckets


def _bucket() -> dict[str, dict[str, float | int]]:
    return {"raw": {"n": 0, "pnl": 0.0}, "clean": {"n": 0, "pnl": 0.0}}


def _add_bucket(bucket: dict[str, float | int], pnl: str | None) -> None:
    bucket["n"] = int(bucket["n"]) + 1
    bucket["pnl"] = round(float(bucket["pnl"]) + float(pnl or 0), 4)


def _bucket_name(edge: Decimal) -> str:
    if edge < Decimal("0.01"):
        return "0-1%"
    if edge < Decimal("0.02"):
        return "1-2%"
    if edge < Decimal("0.05"):
        return "2-5%"
    return ">5%"


def _copy_trade_clv(store: Store, config) -> dict:
    try:
        # K13: no-vig is the default verdict instrument (N1); the fee-loaded metric is
        # documented "BIASED — comparison only" and must not be the number people look at.
        from arb.experimental.ev.clv import clv_result

        result = clv_result(store, fee_loaded=False, min_depth=config.min_executable_usd)
        return {"available": True, **result}
    except Exception as exc:  # noqa: BLE001 - dashboard must remain read-only and best-effort.
        return {"available": False, "error": str(exc)}


_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>ArbitrageModel - read-only dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
<style>body{font-family:system-ui;margin:2rem;max-width:900px}
table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style></head>
<body><h1>ArbitrageModel</h1><p>Read-only. Paper measurement; no orders placed.</p>
<div id="hero"></div>
<h2>Performance by bet type</h2><p style="font-size:.8rem;color:#777">Each type separate; confirmed/clean/flat-$1. Optimistic column is the locked-at-entry mirage (a re-grade of the same bets).</p>
<table id="pertype"></table>
<canvas id="eq"></canvas><canvas id="kelly"></canvas><canvas id="buckets"></canvas>
<h2>Risk</h2><pre id="risk"></pre><h2>ARB grades</h2><pre id="grades"></pre>
<h2>CLV</h2><pre id="clv"></pre><h2>Segment scores</h2><div id="scores"></div>
<script>
fetch('dashboard.json').then(r=>r.json()).then(d=>{
 const lineOptions={
   responsive:true,
   elements:{point:{radius:0,hitRadius:8,hoverRadius:3},line:{borderWidth:2,tension:.18}},
   plugins:{legend:{position:'bottom'}},
   scales:{
     x:{type:'time',grid:{display:false},ticks:{maxTicksLimit:5}},
     y:{grid:{color:'rgba(0,0,0,.08)'},ticks:{maxTicksLimit:6,callback:v=>'$'+Number(v).toFixed(Math.abs(v)>=10?0:2)}}
   }
 };
 const mk=(c,l)=>({label:l,data:c.map(p=>({x:p.t,y:p.cum})),fill:false});
 new Chart(document.getElementById('eq'),{type:'line',data:{datasets:[
   mk(d.equity.optimistic_raw,'optimistic raw'),mk(d.equity.optimistic_clean,'optimistic clean'),
   mk(d.equity.confirmed_raw,'confirmed raw'),mk(d.equity.confirmed_clean,'confirmed clean')]},
   options:{...lineOptions,parsing:false}});
 new Chart(document.getElementById('kelly'),{type:'line',data:{datasets:[
   mk(d.kelly_equity.optimistic_raw,'Kelly optimistic raw'),mk(d.kelly_equity.optimistic_clean,'Kelly optimistic clean'),
   mk(d.kelly_equity.confirmed_raw,'Kelly confirmed raw'),mk(d.kelly_equity.confirmed_clean,'Kelly confirmed clean')]},
   options:{...lineOptions,parsing:false}});
 const labels=Object.keys(d.ev_edge_buckets);
 new Chart(document.getElementById('buckets'),{type:'bar',data:{labels,datasets:[
   {label:'EV raw count',data:labels.map(k=>d.ev_edge_buckets[k].raw.n)},
   {label:'EV clean count',data:labels.map(k=>d.ev_edge_buckets[k].clean.n)}]}});
 document.getElementById('risk').textContent=JSON.stringify({
   drawdowns:d.drawdowns,kelly_drawdowns:d.kelly_drawdowns,
   optimistic_confirmed_gap:d.optimistic_confirmed_gap,left_on_table:d.left_on_table
 },null,1);
 document.getElementById('grades').textContent=JSON.stringify(d.arb_grades,null,1)+
   '\\nopen: '+JSON.stringify(d.arb_open);
 document.getElementById('clv').textContent=JSON.stringify(d.copy_trade_clv,null,1);
 document.getElementById('hero').innerHTML=
   `<div style="font-size:.85rem;color:#666">Total profit (${d.headline.basis})</div>`+
   `<div style="font-size:2.4rem;font-weight:700">$${Number(d.headline.total_profit).toFixed(2)}</div>`;
 let pt='<tr><th>type</th><th>n</th><th>WR</th><th>ROI</th><th>P&amp;L $</th><th>optimistic $ (mirage)</th><th>status</th></tr>';
 d.per_type.forEach(t=>{const wr=(t.win_rate==null)?'-':(t.win_rate*100).toFixed(1)+'%';
   pt+=`<tr><td>${t.kind}</td><td>${t.n}</td><td>${wr}</td><td>${(t.roi*100).toFixed(1)}%</td><td>$${Number(t.pnl).toFixed(2)}</td><td>$${Number(t.versions.optimistic_clean_flat.pnl).toFixed(2)}</td><td>${t.status}</td></tr>`;});
 document.getElementById('pertype').innerHTML=pt;
 let h='<table><tr><th>segment</th><th>n</th><th>WR</th><th>ROI</th><th>score</th><th>tier</th></tr>';
 d.segment_scores.forEach(s=>h+=`<tr><td>${s.segment}</td><td>${s.n}</td><td>${(s.win_rate*100).toFixed(1)}%</td><td>${(s.roi*100).toFixed(1)}%</td><td>${s.score}</td><td>${s.tier}</td></tr>`);
 document.getElementById('scores').innerHTML=h+'</table>';
});
</script></body></html>"""
