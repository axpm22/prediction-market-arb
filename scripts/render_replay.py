"""HTML for the replay panel. Self-contained: no CDN, no JS, inline CSS only."""
from __future__ import annotations

VENUE_COLOR = {"kalshi": "#4c8fd6", "polymarket": "#c9722f", "sx": "#5d9e73",
               "oddsapi": "#8a7bb8"}

CSS = """
:root { --bg:#fbfbfa; --fg:#1c1b19; --dim:#6b6862; --line:#e2e0dc; --card:#fff;
        --warn:#8a5a1a; --warnbg:#fdf5e8; --ok:#2c6e49; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16151a; --fg:#e8e6e1; --dim:#9a968e; --line:#2e2c33; --card:#1e1d23;
          --warn:#e0a458; --warnbg:#2a2118; --ok:#6cc48f; } }
:root[data-theme="dark"] { --bg:#16151a; --fg:#e8e6e1; --dim:#9a968e; --line:#2e2c33;
          --card:#1e1d23; --warn:#e0a458; --warnbg:#2a2118; --ok:#6cc48f; }
:root[data-theme="light"] { --bg:#fbfbfa; --fg:#1c1b19; --dim:#6b6862; --line:#e2e0dc;
          --card:#fff; --warn:#8a5a1a; --warnbg:#fdf5e8; --ok:#2c6e49; }
* { box-sizing:border-box }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif; }
main { max-width:1100px; margin:0 auto }
h1 { font-size:1.5rem; margin:0 0 .25rem; letter-spacing:-.01em }
h2 { font-size:1rem; margin:2.5rem 0 .6rem; text-transform:uppercase;
     letter-spacing:.08em; color:var(--dim); font-weight:600 }
p.sub { color:var(--dim); margin:.2rem 0 0 }
.cards { display:flex; flex-wrap:wrap; gap:.75rem; margin-top:1.25rem }
.card { flex:1 1 150px; background:var(--card); border:1px solid var(--line);
        border-radius:10px; padding:.85rem 1rem }
.card .n { font-size:1.65rem; font-weight:600; font-variant-numeric:tabular-nums;
           letter-spacing:-.02em }
.card .k { color:var(--dim); font-size:.78rem; text-transform:uppercase;
           letter-spacing:.06em; margin-top:.15rem }
.note { background:var(--warnbg); border:1px solid var(--line);
        border-left:3px solid var(--warn); border-radius:6px; padding:.7rem .9rem;
        color:var(--fg); margin:1rem 0; font-size:.9rem }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:10px;
          background:var(--card) }
table { border-collapse:collapse; width:100%; font-size:.87rem }
th,td { padding:.45rem .7rem; text-align:right; white-space:nowrap;
        border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums }
th { text-align:right; color:var(--dim); font-weight:600; font-size:.76rem;
     text-transform:uppercase; letter-spacing:.05em }
th:first-child,td:first-child,td.l,th.l { text-align:left }
tbody tr:last-child td { border-bottom:0 }
tr.outage td:first-child::after { content:" ● PM dark"; color:var(--warn);
     font-size:.72rem; letter-spacing:.03em }
.bar { display:flex; height:15px; border-radius:3px; overflow:hidden;
       min-width:170px; background:var(--line) }
.bar i { display:block; height:100% }
.legend { display:flex; gap:1rem; flex-wrap:wrap; color:var(--dim);
          font-size:.82rem; margin:.6rem 0 0 }
.legend b { display:inline-block; width:10px; height:10px; border-radius:2px;
            margin-right:.35rem }
.zero { color:var(--dim) }
.good { color:var(--ok); font-weight:600 }
footer { margin-top:3rem; color:var(--dim); font-size:.8rem; line-height:1.7 }
code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em }
"""


def _n(x) -> str:
    return f"{x:,}".replace(",", " ")


def _sum(rows, key):
    return sum(r[key] for r in rows)


def _bar(mix: dict) -> str:
    total = sum(mix.values()) or 1
    seg = "".join(
        f'<i style="width:{100 * n / total:.2f}%;background:{VENUE_COLOR.get(v, "#888")}"'
        f' title="{v}: {_n(n)}"></i>'
        for v, n in sorted(mix.items(), key=lambda kv: -kv[1]))
    return f'<div class="bar">{seg}</div>'


def _pnl_section(p: dict | None) -> str:
    """Paper P&L from the ledger. Deliberately leads with the preregistered
    clean-lane number, and puts the big review-lane figure behind its caveat."""
    if not p:
        return ""
    rows = p["rows"]
    tot = {k: sum(float(r[k]) for r in rows) for k in ("stake", "opt", "conf")}
    tot_missed = sum(r["missed"] for r in rows)
    tot_open = sum(r["open"] for r in rows)
    review = [r for r in rows if r["lane"] != "clean"]
    rev_conf = sum(float(r["conf"]) for r in review)
    rev_stake = sum(float(r["stake"]) for r in review)

    out = [f"""<h2>Paper P&amp;L — from the ledger, not from replay</h2>
<p class="sub">Replay is detection-only and cannot produce P&amp;L; these come from
<code>flat_bets</code> + <code>bet_events</code> via <code>metrics.py</code>, the same
path <code>arb report</code> uses. Every stake is $1 of paper money. No order was ever
placed — rule 1.</p>
<div class="cards">
<div class="card"><div class="n">${float(p['headline']):+.4f}</div>
<div class="k">headline: settled, clean lane ({p['headline_n']} bets)</div></div>
<div class="card"><div class="n">${rev_conf:+.2f}</div>
<div class="k">review lane, confirmed basis (not bankable)</div></div>
<div class="card"><div class="n">{tot_missed}</div>
<div class="k">flagged but never filled ($0 each)</div></div>
<div class="card"><div class="n">${tot['stake']:.0f}</div>
<div class="k">total paper stake, {p['first_bet']}–{p['last_bet']}</div></div>
</div>
<div class="note"><b>Read the small number, not the big one.</b> The headline
(${float(p['headline']):+.4f} over {p['headline_n']} bets) is the preregistered figure:
settled, trusted, sports, pregame, clean lane. Everything else here is
<code>lane=review</code> — overwhelmingly XSPREAD, which is cross-venue and
REVIEW-only <em>forever</em> by rule 6, because differing settlement rules between
venues are the main source of fake arbs. ${rev_conf:+.2f} on ${rev_stake:.0f} staked is a
~{100 * rev_conf / rev_stake if rev_stake else 0:.0f}% return over about three weeks; a
genuine locked arb does not pay that. Treat it as the quantity this instrument exists
to <em>doubt</em>, not as foregone profit. {tot_missed} of the flags never got a confirm
event at all — the bet was never made, and enters at $0.</div>
<div class="scroll"><table><thead><tr><th class='l'>lane</th><th class='l'>kind</th>
<th>bets</th><th>stake</th><th>optimistic</th><th>confirmed</th><th>missed</th>
<th>open</th><th>excluded</th></tr></thead><tbody>"""]
    for r in rows:
        clean = r["lane"] == "clean"
        out.append(
            f'<tr><td class="l">{"<b>" + r["lane"] + "</b>" if clean else r["lane"]}</td>'
            f'<td class="l">{r["kind"]}</td><td>{r["n"]}</td>'
            f'<td>${float(r["stake"]):.0f}</td><td>${float(r["opt"]):+.4f}</td>'
            f'<td>${float(r["conf"]):+.4f}</td>'
            f'<td class="{"zero" if not r["missed"] else ""}">{r["missed"]}</td>'
            f'<td class="{"zero" if not r["open"] else ""}">{r["open"]}</td>'
            f'<td class="{"zero" if not r["excluded"] else ""}">{r["excluded"]}</td></tr>')
    out.append(f'<tr><td class="l"><b>all</b></td><td class="l"></td>'
               f'<td>{sum(r["n"] for r in rows)}</td><td>${tot["stake"]:.0f}</td>'
               f'<td>${tot["opt"]:+.4f}</td><td>${tot["conf"]:+.4f}</td>'
               f'<td>{tot_missed}</td><td>{tot_open}</td><td>0</td></tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


def render(d: dict) -> str:
    rows = d["rows"]
    watch = [r for r in rows if r["kind"] == "watch"]
    tot_both = _sum(rows, "both")
    tot_new = _sum(rows, "replay_only")
    tot_gone = _sum(rows, "stored_only")
    faithful = tot_new == 0 and tot_gone == 0
    # PM-dark days are read off the observed quote mix, never a hardcoded window
    dark = {day for day, m in d["mix"].items() if not m.get("polymarket")}
    lit = sorted(set(d["mix"]) - dark)
    resumed = next((x for x in lit if dark and x > min(dark)), None)

    cards = [
        (_n(_sum(rows, "quotes")), "quotes replayed"),
        (_n(_sum(rows, "polls")), "polls re-detected"),
        (f"{d['total_secs']:.0f}s", "wall clock"),
        (_n(tot_both), "flags reproduced"),
        (f'<span class="{"good" if faithful else ""}">{tot_new}</span>', "replay-only (new)"),
        (f'<span class="{"good" if faithful else ""}">{tot_gone}</span>', "stored-only (lost)"),
    ]

    body = [f"""<main>
<h1>Replay panel — {d['from']} → {d['to']}</h1>
<p class="sub">Re-ran <code>detect()</code> over quotes already in SQLite, at each poll's own
clock. Read-only: the hot DB and the archive are both opened <code>mode=ro</code>.
Generated {d['generated_at'][:19]}Z.</p>
<div class="cards">""" + "".join(
        f'<div class="card"><div class="n">{n}</div><div class="k">{k}</div></div>'
        for n, k in cards) + "</div>"]

    body.append(f"""<div class="note"><b>{'Faithful.' if faithful else 'Divergence.'}</b>
{'Current HEAD reproduces the stored history exactly — every flag then is a flag now, and nothing new appeared. That zero is the control: after any detection or threshold change, re-run this and whatever is non-zero <em>is</em> the change.'
 if faithful else
 f'{tot_new} flags the current code would newly make and {tot_gone} it no longer makes. A difference means THE CODE OR CONFIG CHANGED — the stored history is immutable.'}
</div>""")

    body.append(_pnl_section(d.get("pnl")))
    body.append("<h2>Per day and lane</h2><div class=\"scroll\"><table><thead><tr>"
                "<th class='l'>day</th><th>lane</th><th>polls</th><th>quotes</th>"
                "<th>ARB</th><th>XSPREAD</th><th>divergence</th><th>in-play</th>"
                "<th>zombie</th><th>suspect</th><th>both</th><th>new</th><th>lost</th>"
                "<th>secs</th></tr></thead><tbody>")
    for r in rows:
        arb = sum(n for k, n in r["opps"].items() if k.startswith("ARB/"))
        xs = sum(n for k, n in r["opps"].items() if k.startswith("XSPREAD/"))
        dv = sum(n for k, n in r["opps"].items() if k.startswith("DIVERGENCE/"))
        rj = r["rejects"]
        cells = [arb, xs, dv, rj.get("in-play", 0) + rj.get("pregame-in-live-cycle", 0),
                 rj.get("zombie", 0), rj.get("match-suspect-price", 0),
                 r["both"], r["replay_only"], r["stored_only"]]
        body.append(
            f'<tr class="{"outage" if r["day"] in dark and r["kind"] == "watch" else ""}">'
            f'<td class="l">{r["day"]}</td><td>{r["kind"]}</td>'
            f'<td>{_n(r["polls"])}</td><td>{_n(r["quotes"])}</td>'
            + "".join(f'<td class="{"zero" if c == 0 else ""}">{_n(c)}</td>' for c in cells)
            + f'<td>{r["secs"]}</td></tr>')
    body.append("</tbody></table></div>")

    if resumed:
        body.append(f"""<div class="note"><b>Polymarket is quoting again since
{resumed}.</b> PM contributed no quotes at all on {min(dark)}–{max(d_ for d_ in dark)},
then {100 * d['mix'][resumed]['polymarket'] / sum(d['mix'][resumed].values()):.0f}% of the
day's quotes on {resumed}. The draft PREREGISTRATION amendment declares the PM/ANJ window
open-ended, with the end date "to be recorded in a follow-up row" — this is the evidence
for that row. It also reopens the <code>flat_bets id 42</code> reconcile decision before the
<code>reconcile_give_up_days</code> timer fires around 2026-07-30. Operator call, not
this tool's: nothing here amends anything.</div>""")

    body.append(f"""<h2>Venue quote mix — the PM/ANJ outage</h2>
<p class="sub">Share of quotes recorded per venue, by UTC day. Rows before
{d['archive_horizon']} come from the monthly archive
({', '.join(d['archives']) or 'none'}) — a plain aggregate, <b>not</b> a replay:
those quotes are no longer in the hot DB, so <code>arb replay</code> cannot reach them.</p>
<div class="scroll"><table><thead><tr><th class='l'>UTC day</th><th class='l'>mix</th>
<th>quotes</th><th>polymarket</th><th>PM share</th></tr></thead><tbody>""")
    for day, mix in d["mix"].items():
        total = sum(mix.values())
        pm = mix.get("polymarket", 0)
        body.append(
            f'<tr class="{"outage" if day in dark else ""}">'
            f'<td class="l">{day}</td><td class="l">{_bar(mix)}</td>'
            f'<td>{_n(total)}</td><td class="{"zero" if not pm else ""}">{_n(pm)}</td>'
            f'<td class="{"zero" if not pm else ""}">{100 * pm / total if total else 0:.1f}%</td></tr>')
    body.append("</tbody></table></div><div class=\"legend\">" + "".join(
        f'<span><b style="background:{c}"></b>{v}</span>' for v, c in VENUE_COLOR.items())
        + "</div>")

    body.append(f"""<footer>
<b>Scope.</b> Sports ARB / XSPREAD / DIVERGENCE detect path only. The negRisk and
political lanes consume raw books, not quote rows, and are <b>not</b> replayed — a
quiet lane here is not a replayed lane. Nothing downstream of detection is replayed
either: no tracker, no eligibility, no settlement, so nothing on this page is a
claim about P&amp;L.<br>
<b>Reach.</b> <code>arb replay</code> sees only the hot DB, which keeps
<code>archive_after_days</code> of quotes — everything before {d['archive_horizon']}
has moved to <code>data/archive/</code>. The pre-outage baseline therefore cannot be
replayed today, only aggregated as above.<br>
<b>Writes.</b> None. Every connection on this page was opened read-only.
</footer></main>""")

    # http.server sends text/html with no charset; declare it or the em-dashes rot
    return ('<meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Replay panel {d["from"]}..{d["to"]}</title>'
            f"<style>{CSS}</style>" + "".join(body))
