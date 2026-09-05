"""L14 price-band grading: what the band itself paid, taken off every realized
edge BEFORE it is scored (amendment 2026-09-05, operator-directed).

Operator: "Make sure when we grade a whale on 'correct trades' we don't just
take people who are taking easy likely trades."

`whale_score.realized_edge` already grades per share against the fill price, so
a raw win rate never entered — but the fill price's own BAND still paid a
premium nobody earned. Measured on the live tape (sports, pregame, resolved,
entry >= 2026-01-01; n 33,809 positions, 1,052 wallets), mean realized edge by
entry band:

    [0.0,0.2)  +0.0114 (n   653)      [0.6,0.8)  +0.0073 (n 7,126)
    [0.2,0.4)  +0.0276 (n 4,138)      [0.8,0.9)  -0.0193 (n 1,941)
    [0.4,0.6)  +0.0086 (n18,316)      [0.9,1.0]  -0.0257 (n 1,635)

So a wallet that only bought in [0.2,0.4) carried +2.8 points anyone buying
there collected, and a favourite-taker was debited 2.5 for the band, not for
its judgement. Grading fixes that with one subtraction:

    e_adj = e - m_band(root, band_of(avg_price))

Applied by the ONE assembly (`whale_state.score_state`) before `aggregate`, so
`score_for`, V5's elite cutoffs, V6's excess x notional and the V2/V3 excess
weights all read graded skill with no branch of their own. m0 — the EB prior
mean — is then taken off the SAME graded edges, exactly as before, which
centres it near zero: the band table already carries what the root pays.

Thin bands: below `band_min_n` there is no honest band estimate, so the band
falls back to the root's own m0, which leaves such a position exactly where the
pre-amendment score put it. `band_adjust: false` restores that everywhere.

Population: whatever outcomes the caller hands in — the lane and the report
both pass their hygiene-filtered, point-in-time set, so the band rate is drawn
from the same population the score is.

Rule 4: `price_bands`, `band_min_n` and `band_adjust` from `whale.score`.
Rule 5: Decimal sums, q6 at the same boundaries the surrounding code uses.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from arb.models import q6
from arb.whale_score import base_rate


def edges(cfg) -> list:
    """The registered band edges. Already Decimal (arb/config.py parses every
    YAML float that way), so this is a read and not a conversion."""
    return cfg["whale"]["score"]["price_bands"]


def band_of(avg_price, cfg) -> int:
    """The band a fill price sits in, lower edge INCLUSIVE and upper edge
    EXCLUSIVE — which is exactly what `bisect_right` computes. With edges
    [0.2,0.4,0.6,0.8,0.9] that is [0,0.2) [0.2,0.4) [0.4,0.6) [0.6,0.8)
    [0.8,0.9) [0.9,1.0], the last one closed at the top because a fill at 1.0
    has nowhere else to go. n edges make n+1 bands; a price on an edge belongs
    to the band ABOVE it.
    """
    price = avg_price if isinstance(avg_price, Decimal) else Decimal(str(avg_price))
    return bisect_right(edges(cfg), price)


def band_base_rates(outcomes, root, as_of, cfg) -> dict[int, tuple[Decimal, int]]:
    """band -> (what it paid EVERY wallet, n behind that), over the last
    `base_rate_lookback_days` — `base_rate`'s own lookback and `as_of`
    discipline, so a band can no more see the future than m0 can.

    Below `band_min_n` the band reports the root's m0 instead: unknown is not
    zero, and m0 is the reading that leaves the score where it was. Every
    registered band is present, empty ones included, so a caller indexes by
    band without a KeyError.
    """
    s = cfg["whale"]["score"]
    floor = as_of - timedelta(days=int(s["base_rate_lookback_days"]))
    cells = defaultdict(lambda: [0, Decimal(0)])
    for o in outcomes:
        if o.root == root and floor <= o.resolved_at < as_of:
            cell = cells[band_of(o.avg_price, cfg)]
            cell[0] += 1
            cell[1] += o.e
    m0 = base_rate(outcomes, root, as_of, cfg)[0]
    min_n = int(s["band_min_n"])
    out = {}
    for b in range(len(edges(cfg)) + 1):
        n, total = cells[b] if b in cells else (0, Decimal(0))
        out[b] = ((q6(total / n) if n >= min_n else m0), n)
    return out


def table(outcomes, as_of, cfg) -> dict:
    """root -> band -> (m, n), for every root present. EMPTY — hence a no-op
    through `adjust` and `rate` — when `band_adjust` is false, which is what
    restores the pre-amendment score bit-for-bit."""
    if not cfg["whale"]["score"]["band_adjust"]:
        return {}
    return {root: band_base_rates(outcomes, root, as_of, cfg)
            for root in {o.root for o in outcomes}}


def rate(bands, root, avg_price, cfg) -> Decimal:
    """m_band for one fill. Zero when grading is off, and zero for a root the
    table has never seen — a root with no outcomes has nothing to grade
    against, and inventing a rate for it would be the direction that MOVES a
    score on no evidence."""
    cells = (bands or {}).get(root)
    return cells[band_of(avg_price, cfg)][0] if cells else Decimal(0)


def adjust(outcomes, bands, cfg) -> list:
    """Every outcome's `e` replaced by `e - m_band`. The originals are returned
    untouched when grading is off, so `band_adjust: false` cannot even round."""
    if not bands:
        return outcomes
    return [replace(o, e=q6(o.e - rate(bands, o.root, o.avg_price, cfg)))
            for o in outcomes]
