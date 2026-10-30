import json
o = json.load(open("data/corr/open.json")); s = json.load(open("data/corr/settled.json")); c = json.load(open("data/corr/candles.json"))
ns = json.load(open("data/corr/nonsports.json")); sv = json.load(open("data/corr/settled_event_volume.json"))
PROP_RULE = {"NFL": "KXNFLRECYDS", "NCAAF": "KXNCAAFTEAMTOTAL", "NBA": "KXNBAPTS", "NCAAMB": "KXNCAAMBSPREAD", "NHL": "KXNHLGOAL", "EPL": "KXEPLSCORE",
             "ATP": "KXATPSETWINNER", "UFC": "KXUFCMOV", "PGA": "KXPGATOP10"}
L = ["# Phase-0 correlation inventory (2026-10-29 ET)", "",
 "Read-only public GETs via arb.mlb.kalshi_pull.Client. Historical cutoff 2026-08-30 (settled before it -> /historical/*). Raw summaries: `data/corr/inventory.json`.",
 "Caveats: open-event snapshot includes games days away (zero volume yet), so `med vol` there understates; use the settled-event volume table for real liquidity. "
 "Settled counts are capped at 6 pages (1200 events) and 1 page (200) for non-key series, so 'earliest' is a lower bound on depth when 'complete'=no. "
 "Spread = median (yes_ask - yes_bid) over two-sided markets with mid in [3c,97c]. Volume units = contracts (volume_fp).", "",
 "## Series table (all families)", "", "| series | title | fee (type x mult) | open ev | mkts | med spread | med vol (open) | settled ev | earliest | complete |", "|---|---|---|---|---|---|---|---|---|---|"]
for t, r in o.items():
    st = s.get(t, {}); sn = r["snap"]
    L.append(f"| {t} | {r['title'][:34]} | {r['fee_type'][:5]} x{r['fee_mult']} | {r['n_open_events']}{'+' if r.get('open_cursor') else ''} | {sn['n_markets']} | {sn['med_spread']} | {sn['med_volume']} | {st.get('n_settled_counted','-')} | {st.get('earliest_seen','-')} | {st.get('complete','-')} |")
L += ["", "## Non-sports", "", "| series | title | fee | open ev | mkts/open | med spread | med vol | settled ev (counted) | earliest seen |", "|---|---|---|---|---|---|---|---|---|"]
for t, r in ns.items():
    L.append(f"| {t} | {r['title'][:34]} | {r['fee_type'][:5]} x{r['fee_mult']} | {r['n_open_events']} | {r['snap']['n_markets']} | {r['snap']['med_spread']} | {r['snap']['med_volume']} | {r['settled']['n_counted']}{'' if r['settled']['complete'] else '+'} | {r['settled']['earliest_seen']} |")
L += ["", "## Verbatim rules_primary (one prop series per family)", ""]
for fam, t in PROP_RULE.items():
    m = o[t].get("sample_market", {}); L.append(f"- **{t}** (`{m.get('ticker')}`, {m.get('n_in_event')} mkts in that event): {m.get('rules_primary')}")
for t in ("KXHIGHNY", "KXHIGHPHIL", "KXCPI", "KXCPICORE"):
    m = ns[t]["sample_market"]; L.append(f"- **{t}** (`{m['ticker']}`): {m['rules_primary']}")
L += ["", "## Settled-event liquidity and candlestick probes (one settled prop event per family)", "",
      "| probe | series | event | mkts | med vol | p90 vol | max vol | share traded | candle endpoint | 60-min candles (with vol) | span |", "|---|---|---|---|---|---|---|---|---|---|---|"]
import datetime as dt
f = lambda x: dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime("%m-%d %H:%M")
for k, r in c.items():
    v = sv.get(k, {})
    L.append(f"| {k} | {r['series']} | {r['event']} | {v.get('n')} | {v.get('med_vol')} | {v.get('p90_vol')} | {v.get('max_vol')} | {v.get('share_traded')} | {'historical' if r['historical_endpoint'] else 'live'} | {r['n_candles']} ({r['n_with_volume']}) | {f(r['first_end'])} to {f(r['last_end'])} UTC |")
L += ["", "Candle fields: " + ", ".join(c['NFL']['keys']) + ". Candles cover the market's whole open-to-close life (props open 1-6 days pre-game), so pre-game price paths are recoverable for props back to the first settled event.",
      "Fee changes: `/series/fee_changes` returned an empty list (no scheduled changes). All sampled series: quadratic fees; multiplier 1 except MLB 0.5.", ""]
open("data/corr/INVENTORY.md", "w").write("\n".join(L))
print(len(L))
