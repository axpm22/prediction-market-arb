"""Read-only dashboard data — BUILD_PLAN Phase 6.

Queries the ledger and writes reports/dashboard.json (equity curves, segment scores,
grade counts, clean-vs-raw EV). A static reports/dashboard.html (Chart.js from CDN) renders
it. NEVER places trades; pure read. Safe to publish (no secrets in the JSON).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from arb.scoring import score_segments
from arb.store import Store


def build_dashboard_data(store: Store, config) -> dict:
    with store.connect() as con:
        settled = list(
            con.execute(
                """SELECT placed_at, pnl_optimistic, pnl_confirmed, kind, quality_flag
                     FROM flat_bets WHERE status='SETTLED' AND result IN ('WON','LOST','VOID','RESOLVED')
                    ORDER BY placed_at"""
            )
        )
        grade_rows = list(
            con.execute("SELECT status, COUNT(*) n FROM flat_bets WHERE kind='ARB' GROUP BY status")
        )
        arb_open = list(
            con.execute("SELECT COUNT(*) n, COALESCE(SUM(CAST(edge AS REAL)),0) e FROM flat_bets WHERE kind='ARB' AND status='OPEN'")
        )[0]

    def curve(field: str, clean_only: bool) -> list[dict]:
        cum = 0.0
        out = []
        for r in settled:
            if clean_only and r["quality_flag"] is not None:
                continue
            v = r[field]
            if v is None:
                continue
            cum += float(v)
            out.append({"t": r["placed_at"], "cum": round(cum, 4)})
        return out

    scores = score_segments(store, config.scoring)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "equity": {
            "optimistic_raw": curve("pnl_optimistic", clean_only=False),
            "optimistic_clean": curve("pnl_optimistic", clean_only=True),
            "confirmed_raw": curve("pnl_confirmed", clean_only=False),
            "confirmed_clean": curve("pnl_confirmed", clean_only=True),
        },
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
    if not html_path.exists():
        html_path.write_text(_HTML)
    return json_path


_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>ArbitrageModel — read-only dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>body{font-family:system-ui;margin:2rem;max-width:900px}
table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style></head>
<body><h1>ArbitrageModel</h1><p>Read-only. Paper measurement; no orders placed.</p>
<canvas id="eq"></canvas><h2>ARB grades</h2><pre id="grades"></pre>
<h2>Segment scores</h2><div id="scores"></div>
<script>
fetch('dashboard.json').then(r=>r.json()).then(d=>{
 const mk=(c,l)=>({label:l,data:c.map(p=>({x:p.t,y:p.cum})),fill:false});
 new Chart(document.getElementById('eq'),{type:'line',data:{datasets:[
   mk(d.equity.optimistic_raw,'optimistic raw'),mk(d.equity.optimistic_clean,'optimistic clean'),
   mk(d.equity.confirmed_raw,'confirmed raw'),mk(d.equity.confirmed_clean,'confirmed clean')]},
   options:{parsing:false,scales:{x:{type:'time'}}}});
 document.getElementById('grades').textContent=JSON.stringify(d.arb_grades,null,1)+
   '\\nopen: '+JSON.stringify(d.arb_open);
 let h='<table><tr><th>segment</th><th>n</th><th>WR</th><th>ROI</th><th>score</th><th>tier</th></tr>';
 d.segment_scores.forEach(s=>h+=`<tr><td>${s.segment}</td><td>${s.n}</td><td>${(s.win_rate*100).toFixed(1)}%</td><td>${(s.roi*100).toFixed(1)}%</td><td>${s.score}</td><td>${s.tier}</td></tr>`);
 document.getElementById('scores').innerHTML=h+'</table>';
});
</script></body></html>"""
