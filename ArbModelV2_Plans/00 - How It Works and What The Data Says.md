---
title: How It Works & What The Data Says
type: reference
project: ArbitrageModel
status: stable
tags: [arbmodel, practical, data-review, postmortem]
created: 2026-07-01
---

# How It Works & What The Data Says

> [!abstract] What this is
> A practical, stage-by-stage walkthrough of the model, with every claim checked against **three weeks of live data** (10 Jun – 2 Jul 2026: **12,137 polls, 11,789 paper bets, ~2.1 GB of quotes**). For each stage: what it does, what the ledger says about it, and — if it's broken — the concrete fix. Keep what earns its keep; fix what lies. Minimal theory.

Related: [[01 - Current Model - How It Works]] · [[02 - Weaknesses and Lessons]]

---

## 1. The data in one screen

| Class | Rows (unique) | Settled | Headline from the ledger |
|---|---|---|---|
| **ARB** | 1,280 (149) | 315 | Settled P&L ≈ **$0**. Confirmed fill exists for **5 of 1,280**. Re-quoted arbs: **171 vanished / 138 verified**. |
| **EV** | 3,368 | 878 | Win rate **47.6%**, full-sample net **−$20.35**. 100% baseball (consensus is MLB-only). |
| **DIVERGENCE** | 3,412 | — | Logged at $0 stake. Avg spread **5.3%**, max 38.9%. Never bet, by design. |
| **NEGRISK** | 4 | 0 | Every basket summed **> 1** (1.005–1.011). Capturable **$0**. |
| **CLOSE** | 3,725 | — | Lifecycle bookkeeping. |

Three findings drive everything below:
1. **Arbs are one-poll flickers** — the confirm metric is empty because the poll is too slow to see them twice.
2. **The two venue pairs behave oppositely** — kalshi+polymarket arbs vanish ~88% of the time; polymarket+sx arbs almost never do (and that's suspicious for a different reason).
3. **In EV, bigger apparent edge means *worse* outcomes** — a clean signature of contamination, visible directly in the win-rate-by-edge table.

---

## 2. Stage by stage — logic + verdict

### Stage 1 — Pull & normalize to cost per \$1
Each quote (Kalshi book, Polymarket CLOB `/book`, SX orders) becomes **effective cost per \$1 payout** = real ask + venue fee. Kalshi fee is quadratic `rate·P·(1−P)`; Polymarket linear; SX priced at **0**.

> [!success] Verdict: keep — sound foundation.
> One concrete leak: SX's winning-bet fee is unmodeled (`sx.fee_rate: 0.0`). An arb always has exactly one winning leg, so every **polymarket+sx** edge is overstated by that fee. Given 556 of 1,280 arbs are polymarket+sx, this isn't cosmetic.

### Stage 2 — Match the same event (paranoid join)
Per-sport aliases, 6-hour start bucket, **exact line for totals**, and two sanity gates: start-time agreement ≤30 min, and same outcome priced >15 pts apart = matching error.

> [!success] Verdict: keep — it is actively catching bad joins.
> `suspect_matches` holds **998 rejected joins across 89 events**. The gates work. Two holes remain that can still manufacture a fake arb:
> - **Doubleheaders**: same teams + same day hash to one `event_key` before the start-drift gate runs → a game-1 leg can pair with a game-2 leg. *Fix:* cluster legs by start time (≤2 h) before pairing outcomes.
> - **Kalshi start fallback**: when the ticker time won't parse, start falls back to `close_time` (days late), corrupting the event date and the guards. *Fix:* mark `start-unknown`, make non-actionable — never invent a start.

### Stage 3 — Classify (ARB / DIVERGENCE / EV)
`S = Σ covering asks`; arb if `S<1`, `ROI = 1/S − 1`. Divergence if same outcome ≥3 pts apart with no lock. EV if venue cost < de-vigged median of ≥3 books.

> [!warning] Verdict: math is correct; the ARB *population* is mostly noise. See §3 (venue split) and §4 (edge distribution).

The arb edge distribution (edge = `1 − S`, from `optimistic_cost`):

| Edge band | Count | Read |
|---|---|---|
| 0–1% | 410 | Real arbs live here — tiny, as theory predicts. |
| 1–2% | 62 | Plausible. |
| 2–4% | 98 | Getting suspicious. |
| **>4% "trap"** | 98 | **97 of 98 are kalshi+polymarket** — almost all stale/settlement traps (SPEC §12), not money. |

### Stage 4 — Evidence ladder (depth floor → skew label → flash verify → focus → trusted_pairs)
Every flag graded before it counts. The flash verifier re-quotes the exact legs ~10 s later: VERIFIED or VANISHED.

> [!success] Verdict: the single best component — keep all of it.
> The verifier is what converts "we found 138 arbs" into the honest "most weren't real." It paid for itself. **But** it grades the flag and then, historically, the settle/sizing paths ignored the grade (the REVIEW-arb leak — [[02 - Weaknesses and Lessons]] A1). Its output (VERIFIED/VANISHED price) is also thrown away instead of being used as the confirmed fill (see F1). The mechanism is great; it's under-used.

### Stage 5 — Ledger: \$1 per flag, optimistic vs confirmed fill
Append-only `flat_bets`. Two fills: optimistic (first sighting), confirmed (second consecutive sighting). The gap is meant to be slippage.

> [!danger] Verdict: the central metric is missing for arbs and misleading for EV. This is the #1 problem.
> - **Arbs:** confirmed fill exists for **5 of 1,280**. Arbs are seconds long; the ~4.5-min poll never sees them twice.
> - **EV:** where confirmed *does* exist, the price is **flat** — 1,108 of 1,133 paired bets show < 0.5% drift, only 4 got materially worse. So confirmed ≈ optimistic *on the same bet*. The +$39 confirmed vs −$20 optimistic gap is therefore **not** slippage — it is **survivorship**: only bets that persist to a second sighting get a confirmed number, and those are systematically better bets. Reporting confirmed-only totals flatters the strategy through selection, not fill quality.

---

## 3. The venue-pair split (the most actionable new finding)

Arbs split into two populations that behave in **opposite** ways:

| Venue pair | Arbs | Verified | Vanished | Vanish rate | Read |
|---|---|---|---|---|---|
| **kalshi + polymarket** | 676 | 22 | 164 | **~88%** | Genuinely fast, competitive books. Almost every flag is a stale-quote ghost. Also the source of 97/98 ">4% trap" arbs. |
| **polymarket + sx** | 556 | 103 | 5 | **~5%** | Flags *persist* on re-quote — but SX is priced at **0 fee**, its depth is **unknown/thin** (routed to the micro lane), and it has **different void rules**. The persistence is likely SX being slow / un-repriced, not real capturable edge. |
| single-venue (poly/kalshi/sx) | 48 | 13 | 4 | — | Small; includes intra-venue quirks. |

> [!important] Practical implication
> These are two different problems, not one. **kalshi+polymarket**: the edge is real-looking but evaporates — the fix is *speed and confirmation*, and honestly most of it is unbeatable. **polymarket+sx**: the edge looks durable but rests on three unmodeled frictions (SX fee, SX depth, SX settlement rules) — the fix is *pricing SX honestly*, after which most of these likely fail to clear. Do not pool them into one "arb win rate."

---

## 4. The EV cuts (where the signal is and isn't)

**By venue (single-leg EV, settled, optimistic):**

| Venue | n | Win rate | Net |
|---|---|---|---|
| polymarket | 386 | 53% | **+$34.57** |
| sx | 414 | 46% | −$30.98 |
| kalshi | 78 | 32% | −$23.94 |

Only Polymarket EV is positive. SX loses *despite* its zero-fee head start (so it's genuinely bad, not a fee artifact). Kalshi's 32% win rate means the cheap Kalshi asks the model bets on are cheap for a reason.

**By apparent edge (settled, optimistic):**

| Edge (ratio) | n | Win rate | Net |
|---|---|---|---|
| <2% | 602 | **51%** | +$13.47 |
| 2–5% | 189 | 45% | −$7.76 |
| 5–10% | 77 | **31%** | −$24.70 |
| >50% | 4 | 0% | −$4.00 |

> [!danger] The edge is inverted
> Win rate **falls** as apparent edge **rises**. A 5–10% "edge" wins only 31% of the time. This is the fingerprint of adverse selection / stale books: the fatter the edge looks, the more likely you're the only one at a wrong price. The model's own ranking (sort by edge, bet the biggest) is sorting *toward* the losers. Any EV sizing must down-weight, not up-weight, large edges — or gate them out entirely.

**Coverage:** all 878 settled EV bets are **baseball_mlb** and **pregame** (`live=0`). Consensus is fetched for MLB/NBA only, and NBA is out of season — so "EV across 5 sports" is really "EV on MLB." Scope the claim to what's covered.

**Day to day:** choppy, no trend; one −$47.80 day (23 Jun) accounts for more than the entire net loss. Small-sample, event-driven.

---

## 5. Settlement & housekeeping

- **348 arbs are permanently OPEN**, average age **11 days**, never settled — because there is no Polymarket/SX leg resolver, so reconcile skips them ([[02 - Weaknesses and Lessons]] A2). A third of the arb book is stuck. *Fix:* write `polymarket_leg_result` + `sx_leg_result`; until then, don't count OPEN arbs as anything.
- **UNVERIFIABLE arbs average 19 days old** — old SX legs from before the SX re-pricing fix. Cohort effect; fine now.
- **Annotations used correctly**: 8 bets excluded via the immutable annotation table for `bug:in-play-pricing`. The append-only correction path works as intended.
- **Divergence** is doing its job: 3,412 spreads logged at $0, 250 of them >10% (likely residual mismatches worth mining, not trading).

---

## 6. The UX/UI — how we actually look at this

Four surfaces read the same append-only ledger. All are **read-only** on the data plane; none can place a trade.

| Surface | What it is | When you use it |
|---|---|---|
| **`./status.sh`** | One-shot terminal report (bash + `sqlite3`): process health (flags duplicate/missing workers — duplicates = double-counted bets), then per-class counts (ARB flat-$1 OPEN / VERIFIED / VANISHED / UNVERIFIABLE / hot lifetimes), EV CLV verdict, health checks. Takes an optional baseline id to window a test run. | Quick "is everything alive and what's the tally" from the terminal. |
| **`python -m arb report` / `report --daily [date]`** | `rich` tables recomputed from `flat_bets` (idempotent), plus daily files written to `reports/daily/YYYY-MM-DD.{csv,md}` — one CSV row per bet, MD summary. | The honest daily answer; the CSV is the audit trail. |
| **`reports/dashboard.html`** + `dashboard.json` | Static Chart.js page over a JSON dump built by `dashboard.py`: equity curves, drawdowns, the mirage gap, EV edge buckets, segment scores, arb grades. | A shareable snapshot (no secrets in the JSON). |
| **`webserver.py` → `:8787` `/api/state`** | Live local control center: process health, pipeline, equity, segment effectiveness, recent bets, log tails, and the strategy/version toggles ([[DASHBOARD_PROMPT]]). | The real cockpit — live state + turning strategies/versions on and off. |

**The viewing hierarchy** (per the dashboard spec): **hero total → per-bet-type cards (ARB / EV / DIVERGENCE / CLOSE / NEGRISK / LIVE_BASEBALL_EV, each with $ + ROI% + win rate + n + sparkline, never blended) → version A/B panels → equity-over-time graph (toggle per type / per version) → recent bets + log tails.**

### The "honest variant" matrix (pervades every view)
Every P&L number is selectable along three axes, so the UI can never quietly show the flattering one:

- **optimistic vs confirmed** (first sighting vs second — the mirage gap),
- **raw vs clean** (before/after the quality layer removes contaminated edges),
- **flat-$1 vs quarter-Kelly** (unweighted vs weighted sizing).

`dashboard.json` carries all of it: `equity` and `kelly_equity` each hold the four opt/conf × raw/clean series, plus `drawdowns`, `kelly_drawdowns`, and `optimistic_confirmed_gap`. The **default headline is confirmed / clean / flat-$1** and is labeled as such.

### Weighted vs unweighted — the A/B panels (and what the data shows)

> [!note] The concept of weighted ARB (this is the direction to build toward)
> Flat-$1 answers "was the edge real?" It deliberately ignores *how much you could have made*. **Weighted ARB** answers the money question: size each arb by **edge × depth**, not a flat dollar. The logic:
> - A locked arb's profit grows with stake **until the stake walks the book far enough that the summed cost crosses $1** and the lock breaks. That crossing point is set jointly by the **edge** (how far below $1 the basket starts) and the **depth** (how much size sits at each price level). Fat edge + deep book ⇒ size up hard; thin edge or thin book ⇒ barely size at all.
> - Practically: `stake = min( profit-maximizing size from the ladder, min-leg depth × safety_frac, bankroll × max_stake_pct )`. This is exactly the built-but-dormant `ladder_scaled_stake` / `profitmax_stake_usd` path.
> - **Why it matters:** flat-$1 will call a 0.3%-edge/$40-deep arb and a 0.8%-edge/$2,000-deep arb "the same bet." They are not. Weighted ARB is how you rank and size toward the ones actually worth capital, and it's the only way the "how aggressive before I lose?" question is answerable.
> - **Kelly-for-arb** is the same idea expressed as a fraction of bankroll driven by edge and lock-safety; it's the second weighted axis the dashboard is meant to show beside flat-$1.

> [!danger] …but there is no data behind it yet — turn it on in the right order
> In the ledger **every weighted-ARB column is NULL for all 1,280 arb rows** (`kelly_stake_usd`, `kelly_f_star`, `pnl_kelly_optimistic/confirmed`). The feature is gated behind `arb_size_by_ladder: false` and is **degenerate until real multi-level ladders are captured** ([[02 - Weaknesses and Lessons]] B1): on a single flat rung, profit is linear in stake and the lock never crosses, so the model literally cannot see the losing region. **Correct sequence:** (1) capture real ladders (Polymarket per-token `/book`, normalized depth), (2) confirm the capacity report shows genuine *edge*-limited arbs, (3) *then* enable weighted sizing with a conservative `safety_frac`. Until step 1, the dashboard's "weighted ARB vs unweighted" panel compares a real column against an empty one — so populate it in this order, don't just unhide it.

> [!warning] Weighted EV is populated — and shows how Kelly distorts
> For EV the Kelly columns *are* filled, and they magnify both the truth and the illusion:
> | Basis | Flat-$1 | Quarter-Kelly |
> |---|---|---|
> | Optimistic (honest, full sample) | −$20.35 | **−$78.37** |
> | Confirmed (survivorship-biased) | +$39.42 | **+$489.16** |
>
> On the honest optimistic basis, weighting *quadruples the loss*; on the confirmed basis it inflates the survivorship illusion ~12×. **Takeaway for the UI:** never let "Kelly + confirmed" be shown as a standalone total without the survivorship + weighting warnings — it is the single most misleading cell in the whole dashboard.

The second EV weighting axis is **sharp-book-weighted consensus vs equal-weight median** (`weight_sharp_books`, `sharp_books: [lowvig, betonlineag, betus]`). This is a **re-grade of the same bets**, not a new set — and the UI must label it that way. The spec is explicit: a version row is either *distinct bets* or a *re-grade under a different rule*, and a re-grade must never be presented as independent evidence.

### UX shortcomings to fix
- **The static export is stale** — `dashboard.json` is dated 21 Jun (10 days old). Consolidate onto the one live surface (`:8787`) so nobody reads a frozen snapshot as current.
- **Empty weighted-ARB panel** (above) — hide or populate; don't show a half-empty A/B.
- **Kelly + confirmed needs a guardrail** — default it off, badge it, or refuse to headline it.
- **Per-type ARB card should split by venue pair** — kalshi+poly (88% vanish) and poly+sx (5% vanish) are different animals (§3); one blended "ARB win rate" card hides the only actionable arb finding.

---

## 7. APIs & data feeds — what we call, and the fastest option

Everything is **public reads only** (hard rule #1). Here is what the adapters actually hit today, and whether it's the fastest/best available.

| Source | Endpoint we use (REST) | Role | Fastest/best available | Verdict |
|---|---|---|---|---|
| **Kalshi** | `api.elections.kalshi.com/trade-api/v2` — `/markets`, `/markets/{t}/orderbook` (polled) | Primary venue book | **WebSocket** `wss://api.elections.kalshi.com/trade-api/ws/v2`, `orderbook_delta` channel — subscribe once, keep a live local book | **Upgrade.** Correct REST base, but polling is the bottleneck. WS handshake wants signed `KALSHI-ACCESS-*` headers (read-only; no order path) — a small deviation from "unauthenticated," weigh it. |
| **Polymarket** | `gamma-api.polymarket.com/events` (discovery) + `clob.polymarket.com/book` (executable, polled per token) | Primary venue book | **WebSocket** `wss://ws-subscriptions-clob.polymarket.com/ws/market` — subscribe by `token_id`, get `book` + `price_change` + `last_trade_price`; **keyless**. Ping 5 s / pong ≤10 s | **Upgrade, cleanest.** Same CLOB data, pushed, no key. Also gives the per-token ladder that weighted ARB (§6) needs. |
| **SX Bet** | `api.sx.bet` — `/markets/active`, `/orders` (polled) | Primary venue book | **WebSocket** (Centrifugo) `wss://realtime.sx.bet/connection/websocket`, channel `order_book:{token}:{marketHash}`. **Note: Ably was deprecated 1 Jul 2026;** realtime now needs a token from `/user/realtime-token` (**API key required**) | **Upgrade with a caveat.** Realtime is no longer keyless — decide if a read-only SX key is acceptable. REST `/orders` stays the keyless fallback. |
| **The Odds API** | `api.the-odds-api.com/v4` (keyed, credit-limited, 3×/day) | Sportsbook consensus for EV | Fine as-is for a free tier; paid feeds (e.g. OpticOdds) add Pinnacle/Circa + lower latency | **Keep.** Not latency-critical (pregame, cached). The real gap is *quality* — no sharp books — which is EV#2, not speed. |
| **MLB Stats API** | `statsapi.mlb.com/api/v1/schedule` | Final scores / settlement | Official, free, authoritative | **Keep.** Best free source; no faster/better option. |

> [!important] The one API change that attacks the mirage directly
> Detection currently reads books by **REST polling** (30 s timeouts; ~4.5 min daemon sweep, 60 s watch, 10 s verify). But **55% of kalshi+polymarket arbs vanish within ~10 s** (§3) — the system is structurally too slow to see, let alone act on, the thing it hunts. **All three venues expose public WebSocket orderbook feeds.** Moving book reads from polling to WS deltas (maintain a live local book per venue) is the highest-leverage speed change available: it makes detection compare genuinely *simultaneous* prices instead of quotes captured up to a minute apart, which is the root cause of both the vanish rate and the empty confirmed-fill column (F1). REST stays as the fallback and for discovery/settlement.

> [!note] Caveat on "fastest = authenticated"
> The fastest feeds for Kalshi (signed handshake) and SX (realtime token) now involve credentials, even though they carry **read-only, public market data and touch no order endpoint**. That's still consistent with "paper only / no orders," but it does relax the stricter "unauthenticated GETs only" rule. Polymarket's market channel remains fully keyless. Decide per-venue whether a read-only key is acceptable; if not, Polymarket WS + Kalshi/SX REST is still faster than today via connection reuse and tighter poll intervals.

---

## 8. Keep / Fix summary

> [!success] Keep unchanged — these earned it
> Unit conversion + exact-line matching + sanity gates (998 bad joins caught). The flash verifier. Append-only ledger + persist-every-quote (2 GB backtest fuel). The quality layer (EV contamination down to 4 suspect + 3 stale of 878). Disciplined nulls (neg-risk empty, EV disabled). The immutable annotation mechanism.

> [!danger] Fix — ranked by payoff
> **F1 — Use the flash verifier as the confirm source.** Write its ~10 s re-quote price into `confirmed_cost`. Instantly gives arbs the metric they're missing (5→most), for free.
> **F2 — Headline the full-sample optimistic P&L; report confirmed only as a paired delta.** The confirmed-only total is survivorship (drift is flat; §2 Stage 5). Never show it standalone.
> **F3 — Split arbs by venue pair everywhere.** kalshi+poly (88% vanish) and poly+sx (5% vanish, unmodeled frictions) are different animals; one pooled "arb win rate" is meaningless.
> **F4 — Price SX honestly** (winning-bet fee + real/thin depth). Most poly+sx "durable" arbs likely fail after this.
> **F5 — Invert EV edge handling.** Bigger edge → lower win rate; gate out or shrink large edges instead of ranking toward them. Scope EV claims to MLB (the only covered sport). Prefer Polymarket EV (the only positive venue) for any future test.
> **F6 — Close the settlement gap.** Poly/SX leg resolvers so the 348 stuck OPEN arbs resolve; dedup to `position_key` (1,280 rows = 149 positions) before any aggregate; store `edge_points` consistently (currently unpopulated on ARB, so the ledger can't even bucket arb edges).
> **F7 — Fix the UX honesty gaps.** Populate or hide the empty weighted-ARB panel; guardrail the Kelly+confirmed cell (the most misleading number in the dashboard); split the ARB card by venue pair; retire the stale static export in favor of the live `:8787` surface.
> **F8 — Move book reads to WebSocket (§7).** Polymarket WS (keyless) first, then Kalshi/SX. This is the biggest speed lever and it's what makes weighted ARB (real ladders) and honest confirmation (F1) possible. It attacks the 55% vanish rate at the root.

---

## 9. Bottom line

The instrument is good and it already produced the real finding: **riskless cross-venue arb is a mirage** (88% vanish on the fast pair; ≈$0 settled) and **de-vigged EV doesn't beat the vig** (−$20 honest, and the edge is inverted). Two metrics currently lie — the empty arb confirm column and the survivorship-flattered EV confirmed total — and both have cheap fixes (F1, F2). Fix those, split arbs by venue pair, price SX honestly, and the harness keeps doing the one thing it's genuinely good at: **disproving edges cheaply and honestly.**
