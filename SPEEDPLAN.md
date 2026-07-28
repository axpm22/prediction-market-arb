# Speed plan — 2026-07-28

Every number below is measured on this machine from stored data, not estimated.
Sources: `poll_venues.latency_ms` (86k rows), `polls.started_at` (45.7k polls),
`flat_bets.leg_skew_secs` (126 flags), the L5 lead-lag study, and the 47k-request
Kalshi corpus pull run today.

---

## 0. The finding that reframes the question

**The pipeline is not slow. The cadence is a policy choice, and nobody has
turned the dial.**

| what | measured | verdict |
|---|---|---|
| cross-leg skew at detection | median **0.69 s**, p90 1.49 s, max 5.81 s | excellent; 0% breach the 10 s gate |
| venue fetch latency (median) | kalshi 573 ms, PM 249 ms, SX 759 ms | fine |
| achieved cycle period | median **60.2 s**, p90 61.9 s | hits its 60 s target |
| work per cycle | ~15–20 HTTP requests, ~1–2 s wall clock | **the loop sleeps ~58 of every 60 s** |

So there is no hot loop to optimise, no N+1 to fix, no serial fan-out to
parallelise — `polymarket.fetch_markets` already threads per sport, and 0.69 s
skew across three venues proves the fan-out works. Chasing microseconds inside
the cycle would buy nothing.

**We are using ~2% of the wall-clock budget and ~1% of the rate-limit headroom.**

## 1. Why it still matters that we are at 60 s

Two measurements say 60 s is slower than the phenomenon:

- **L5**: Polymarket leads Kalshi by a **27.9 s median**. We sample at 60 s —
  slower than the lag we are trying to trade. Most of the lead is gone before we
  look again.
- **Confirm rate**: only **4 of 7** flags in the last 24 h survived a re-quote,
  and 59–88% across the full history. Roughly **40% of detections run away**
  before they could be acted on.
- **L1** is explicitly a *censored* lower bound: a violation that opens and
  closes inside one 60 s gap is invisible, and no amount of stored history
  recovers it.

The prize is not lower latency per detection. It is **seeing the detections we
currently never see at all**, and catching more of the 40% that vanish.

## 2. The real constraint

Cadence and coverage consume the same budget:

```
requests/sec  =  requests_per_cycle x coverage_multiple / cadence_seconds
```

Today: ~20 x 1 / 60 = **0.33 req/s**.

Observed limits (2026-07-28): a bare loop at ~3 req/s took a 429 within two
calls; a paced client at **~2.3 req/s sustained 47,000 requests with 3 total
429s**. Treat ~2 req/s per IP as the safe sustained ceiling, unauthenticated.

That gives a hard frontier:

| cadence | 1x coverage | 5x | 10x | 40x |
|---|---|---|---|---|
| 60 s | 0.33 | 1.7 | 3.3 ⚠ | 13 ✗ |
| 30 s | 0.67 | 3.3 ⚠ | 6.7 ✗ | 27 ✗ |
| 10 s | 2.0 ⚠ | 10 ✗ | 20 ✗ | 80 ✗ |

✗ = over budget on one IP. **You cannot have both faster and wider on one
machine.** Every tier below is a way of buying out of that table.

## 3. The plan, in tiers

### Tier 0 — free, this week, no architecture change

**0.1 Drop the global cadence 60 s → 20 s.** Lands at 1.0 req/s, still half the
safe ceiling. Expected: the 27.9 s cross-venue lead becomes visible instead of
aliased; L1's censoring window shrinks 3x.
*Cost:* 3x quote volume (see §4 storage). *Risk:* low. *Reversible:* one config key.

**0.2 Stop paying the pre-event wait.** 1.05 of the 2.83 days of capital lockup
is spent waiting for the game to start. Entering near event start cuts lockup
~1.6x and shrinks the window in which the flag can evaporate.
*This is a strategy change, not a speed change, but it buys the same thing.*

**0.3 Kill the p90 tail, not the median.** PM p90 is 7.4 s and max 281 s against
a 249 ms median; SX p90 18.3 s. One slow venue stretches the whole cycle.
Tighten `venue_fetch_timeout_secs` (15 s) and let `PartialFetch` do its job —
the architecture already persists partials and marks the venue not-ok.
*Expected:* p90 cycle 61.9 s → close to median. *Risk:* more partial polls;
watch the venue-ok rate.

### Tier 1 — tiered cadence (the highest value/effort item)

Not every market deserves the same attention. Split the loop:

- **Hot list** (~50–200 markets): cross-listed on both venues, liquid, event
  within N hours. Poll at **5–10 s**.
- **Warm** (everything else in coverage): poll at **60–120 s**, purely to keep
  rule 7's archive and to promote into hot.
- **Promotion rule**, pre-registered: a market enters hot when it is cross-listed
  AND depth on both legs > floor AND event start within the window.

Budget: 200 hot markets, batched (PM `POST /books` takes many tokens per call;
Kalshi markets-listing returns 1000/page) ≈ 5–8 requests per hot cycle → at 10 s
that is 0.8 req/s. **Leaves room for 40x wider warm coverage in the same budget.**

The `hot` lane already exists in config (`hot.poll_secs`, `check_interval_secs`,
`max_watchers`) and in the CLI. This is largely wiring what is already there.

### Tier 2 — architecture: stop re-fetching what has not changed

**2.1 Streaming where it exists.** Kalshi publishes a WebSocket API; SX's was
deprecated 2026-07-01. **Pin first** (rule 3): confirm the endpoint, auth
requirements (must be keyless to satisfy rule 1), channels and message shape,
and record it in `data/verified_facts.md` before any code. If Kalshi's WS is
keyless for market data, the Kalshi leg's cadence problem disappears entirely —
push replaces poll and the request budget stops binding on that venue.

**2.2 Conditional fetch.** Use `updated_time`/cursors to skip unchanged markets
rather than re-pulling the world every cycle. Cuts requests per cycle, which is
the numerator of the whole frontier.

**2.3 Break the cron-not-daemon rule, deliberately.** STRATEGIES.md flags a
minute-cadence collector as an explicit infra decision (L7's subsidy leg needs
it too). A long-lived collector process keeps connections warm, holds WS
subscriptions, and removes per-cycle process startup. This is the one item that
changes the shape of the system — it wants its own decision, not a config flag.

### Tier 3 — horizontal: more IPs

Compute was never the bottleneck (47k requests, one laptop, 3 rate limits).
**IP address is the scarce resource**, because the limit is per-origin. N hosts
≈ N x the request budget, partitioned by venue or by series so each host owns a
disjoint slice and results merge into one store.

*Only worth doing after Tier 1*, because tiered cadence buys most of the same
headroom without the distributed-systems cost (clock skew across hosts would
directly corrupt `leg_skew_secs`, which is currently our cleanest measurement at
0.69 s — see the risk in §5).

## 4. What breaks when you go faster

**Storage.** 8.4 M quotes in 20 days at 60 s. Linear in cadence and coverage:

| | 60 s | 20 s | 10 s |
|---|---|---|---|
| 1x coverage | 8 M / 20 d | 25 M | 50 M |
| 10x coverage | 84 M | 250 M | 500 M |

Rule 7 requires persisting every quote. At Tier 1 scale that is a real design
problem, and the answer is *not* to break rule 7. Options, in order of
preference: keep full `ladder_json` only for hot-list markets (ladders are the
bulk — PM stores a median 22 levels); shorten `archive_after_days`; move the
archive to compressed monthly files. Decide before switching Tier 1 on, not after.

**Rate limits.** The frontier table in §2 is the budget. Add a global token
bucket shared across lanes so the hot loop, the warm loop and any corpus pull
cannot collectively exceed it — today nothing coordinates them, and the corpus
pull competed with the tape pull for exactly this reason.

**Detection quality.** Faster polling makes `leg_skew_secs` *better*, not worse,
so the actionable-skew gates stay valid. But more polls mean more flags, and
`reappearance-storm` (73 rows) and `position-dedup` already exist for a reason —
dedup by `position_key` is what keeps a 3x cadence from looking like 3x the
opportunities. The banksim and bench both dedup; keep it that way.

**Cost of being wrong.** Nothing above changes the settlement-mismatch bound
(≤7.3% against a 2.3% break-even). Speed multiplies whatever the edge is,
including a negative one.

## 5. Validation — how to know each tier worked

Each step ships with the measurement that proves or kills it:

| step | metric | success looks like | source |
|---|---|---|---|
| 0.1 cadence 20 s | confirm rate | rises from ~57% | `bet_events.confirmed` |
| 0.1 | L1 prevalence | rises (less censoring) | `arb bench --only L1` |
| 0.3 timeout tighten | p90 cycle period | approaches median | `polls.started_at` |
| Tier 1 hot list | distinct arbs/day | rises; **skew stays < 3 s** | `banksim`, `leg_skew_secs` |
| Tier 1 | utilisation | rises from 39–54% | `arb banksim` |
| Tier 2 WS | requests/cycle | falls while cadence falls | `poll_venues` |
| Tier 3 | clock skew across hosts | < 1 s, else abort | rule 14, one master clock |

**Tripwires.** Abort and revert if: 429 rate exceeds 1 per 1,000 requests; venue
ok-rate drops below its current level; `leg_skew_secs` p90 exceeds 3 s; or
DB growth outruns the archive schedule.

## 6. Sequencing and expected gain

| # | step | effort | expected gain | risk |
|---|---|---|---|---|
| 1 | 0.3 tail timeouts | hours | p90 cycle -60% | low |
| 2 | 0.1 cadence 60→20 s | hours + storage decision | 3x less censoring | low |
| 3 | Tier 1 hot/warm split | days | 6x on hot, 40x wider warm | medium |
| 4 | 2.2 conditional fetch | days | cuts the frontier numerator | low |
| 5 | 2.1 Kalshi WS (pin first) | days | removes Kalshi from the budget | medium |
| 6 | 2.3 daemon collector | weeks | enables everything above cleanly | high — architecture |
| 7 | Tier 3 multi-IP | weeks | N x budget | high — clock discipline |

**Do 1 and 2 first.** They are config-scale changes with measurable outcomes,
and their results tell you whether faster actually catches more before you spend
weeks on Tier 2/3. If cadence 20 s does not move the confirm rate, the runaway
problem is not latency and the rest of this plan is wrong.

---

*Provenance: measured 2026-07-28 from `data/arb.sqlite` (45.7k polls, 86k venue
fetches, 126 locked flags) and the Kalshi corpus pull (47k requests). No number
here is an estimate unless labelled as one.*
