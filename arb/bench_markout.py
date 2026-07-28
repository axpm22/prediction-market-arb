"""Markout lanes (L7 maker bounds, L11 large-print follow) over the Kalshi tape
and candle corpus.

Both ask the same shape of question — if we had been on one side of a fill, what
did the price do next — and both are bounded rather than estimated, because the
fill assumption is the part we cannot verify retrospectively. Each lane states
which direction its assumption is generous in, so a negative result is trustworthy
even where a positive one would only be an upper bound.
"""
from __future__ import annotations

import json
import statistics
from decimal import Decimal

from arb.bench import LaneResult, verdict_for
from arb.bench_kalshi import KALSHI_FEE_CATEGORY, _candles, _d
from arb.models import q6
from arb.venues.fees import leg_fee_cost


def l7_maker(cfg, con, hist) -> LaneResult:
    """L7 — maker markout bracket. Conservative leg only: we count a fill only
    when the hour's trade range printed strictly THROUGH a resting bid at the
    previous hour's touch, which is the one fill assumption that does not grant
    us queue priority we cannot prove. The optimistic (fill-at-touch) leg needs
    per-trade tape, so the bracket is one-sided today and labelled as such —
    a one-sided bracket bounds nothing, hence the note."""
    r = LaneResult(lane="L7", name="maker bounds (Kalshi)",
                   primary="conservative-fill maker markout per contract, "
                           "unit = fill-hour",
                   unit="fill-hour")
    if hist is None:
        r.note = "no Kalshi corpus: run `arb bench --pull` (keyless, 0 credits)"
        return r
    marks, clusters = [], set()
    for row in hist.execute(
            "SELECT m.event_ticker, m.category, c.candles_json FROM hist_markets m "
            "JOIN hist_candles c USING (ticker) WHERE c.n >= 3"):
        cs = _candles(row)
        for i in range(1, len(cs) - 1):
            bid = _d((cs[i - 1].get("yes_bid") or {}).get("close_dollars"))
            low = _d((cs[i].get("price") or {}).get("low_dollars"))
            nxt = _d((cs[i + 1].get("price") or {}).get("mean_dollars"))
            if bid is None or low is None or nxt is None or bid <= 0:
                continue
            if low < bid:                       # printed through us: we are filled
                fee = leg_fee_cost(cfg, "kalshi",
                                   KALSHI_FEE_CATEGORY.get(row["category"], "other"),
                                   bid, Decimal(1), "paper", "maker")
                marks.append(float(nxt - bid - fee))
                clusters.add(row["event_ticker"])
    if not marks:
        r.note = "no conservative-fill hour in the corpus"
        return r
    r.n = len(marks)
    nc = len(clusters) or r.n
    mean = statistics.fmean(marks)
    sd = statistics.stdev(marks) if len(marks) > 1 else 0.0
    half = float(cfg["bench"]["ci_z"]) * sd / (nc ** 0.5) if nc else 0.0
    r.estimate, r.lo, r.hi = q6(Decimal(mean)), q6(Decimal(mean - half)), q6(Decimal(mean + half))
    r.note = ("one-sided: the optimistic fill-at-touch leg needs per-trade tape "
              "(keyless /markets/trades, ~1 request per market) — until it exists "
              "this is a lower bound, not the bracket STRATEGIES.md L7 specifies")
    r.detail.append(f"{r.n} conservative fills across {nc} events; "
                    f"maker fee charged at 25% of taker (B0)")
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r


def l11_prints(cfg, con, hist) -> LaneResult:
    """L11 — large-print markout on the Kalshi tape. The question is narrow and
    falsifiable: does SIZE predict direction here?

    The v1 design was scrapped because it granted the follower the whale's own
    fill, which is fiction — by the time a print is public it is already done.
    So the follower is entered at the NEXT print after the large one, which is
    the first price actually available to someone reacting to the tape, and
    marked out `markout_trades_ahead` prints later.

    Honest limitation, stated rather than buried: the next print approximates
    the post-print touch. A true touch needs book snapshots at trade resolution,
    which no free retrospective feed carries. That approximation is generous to
    the strategy (a real follower crosses a spread the next print may not show),
    so a negative result here is safe and a positive one is an upper bound."""
    r = LaneResult(lane="L11", name="large-print markout", unit="print",
                   primary="after-fee follower markout per contract on large prints")
    if hist is None:
        r.note = "no Kalshi corpus: run `arb bench --pull` (keyless, 0 credits)"
        return r
    pct = float(cfg["bench"]["large_print_percentile"]) / 100
    ahead = int(cfg["bench"]["markout_trades_ahead"])
    rows = hist.execute(
        "SELECT m.event_ticker, m.category, t.trades_json FROM tape.hist_trades t "
        "JOIN hist_markets m USING (ticker) WHERE t.n > ?", (ahead * 2,)).fetchall()
    if not rows:
        r.note = ("no tape cached: the corpus has candles but no trades. "
                  "Run the tape leg (keyless GET /markets/trades, 1 request per market).")
        return r
    marks, clusters, prints = [], set(), 0
    for row in rows:
        trades = json.loads(row["trades_json"] or "[]")
        # the endpoint returns newest-first; a markout only means anything forward
        trades = sorted(trades, key=lambda x: x.get("created_time") or "")
        sizes = sorted(float(t.get("count_fp") or 0) for t in trades)
        if not sizes:
            continue
        cutoff = sizes[min(int(pct * len(sizes)), len(sizes) - 1)]
        if cutoff <= sizes[0]:
            # degenerate size distribution: the p90 sits at the minimum, so
            # every print would qualify as "large" and the lane would measure
            # plain tape drift instead of a size effect
            continue
        for i, t in enumerate(trades):
            if float(t.get("count_fp") or 0) < cutoff or i + 1 + ahead >= len(trades):
                continue
            entry = _d(trades[i + 1].get("yes_price_dollars"))
            mark = _d(trades[i + 1 + ahead].get("yes_price_dollars"))
            if entry is None or mark is None or not (0 < entry < 1):
                continue
            # follow the taker: a 'yes' taker bought YES, so we buy YES too
            drift = (mark - entry) if t.get("taker_side") == "yes" else (entry - mark)
            fee = leg_fee_cost(cfg, "kalshi",
                               KALSHI_FEE_CATEGORY.get(row["category"], "other"),
                               entry, Decimal(1), "paper", "taker")
            marks.append(float(drift - fee))
            clusters.add(row["event_ticker"])
            prints += 1
    if not marks:
        r.note = f"tape cached for {len(rows)} markets but no print cleared the cutoff"
        return r
    r.n = prints
    nc = len(clusters) or r.n
    mean = statistics.fmean(marks)
    sd = statistics.stdev(marks) if len(marks) > 1 else 0.0
    half = float(cfg["bench"]["ci_z"]) * sd / (nc ** 0.5) if nc else 0.0
    r.estimate = q6(Decimal(mean))
    r.lo, r.hi = q6(Decimal(mean - half)), q6(Decimal(mean + half))
    r.detail.append(f"{prints} large prints (>= p{int(pct * 100)} of size within their "
                    f"own market) across {nc} events, tape for {len(rows)} markets")
    r.detail.append(f"follower entered at the next print, marked out {ahead} prints later, "
                    f"taker fee charged at entry")
    r.note = ("follower entry approximates the post-print touch with the next print — "
              "generous to the strategy, so a negative result is safe and a positive "
              "one is an upper bound")
    r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
    return r
