"""L14 paper stake sizings (design §8) — the three registered ways one fired
signal is staked, and the one place that says what those three are.

PURE, like arb/whale_signal.py, which is where `conf_stake` used to live (moved
2026-09-05 on size alone when `kelly_stake` joined it): no sqlite, no network,
no clock. Rule 4: every number comes from `cfg["whale"]`. Rule 5: Decimal
throughout, q6 at the output.

- **flat**  $1, the primary. V2/flat is the registered primary metric and
  nothing here may move it.
- **conf**  linear in the signal's whale score `S`, min_stake at the
  qualification bar to max_stake `cap_points` above it. Bounded, no Kelly.
- **kelly** (registered 2026-09-05) fractional Kelly on the SAME edge, but
  priced: a binary contract bought at `p` whose true probability is `p + e`
  has Kelly fraction `e / (1 - p)`, so a rich ask stakes more than a cheap one
  for the same edge. `e` is already a pessimistic lower bound (`score_lb`
  minus the bar), so `kelly_fraction` (0.25) makes it conservative twice.
"""
from __future__ import annotations

from decimal import Decimal

from arb.models import q6
from arb.whale_merge import d

# The registered sizings, in booking order (§8). The lane loops over this and
# `stake_for` dispatches on it — one list, not one per caller.
SIZINGS = ("flat", "conf", "kelly")


def conf_stake(S, cfg) -> Decimal:
    """Spec §8, pinned: min_stake at the qualification bar, max_stake at
    `cap_points` above it. Bounded, monotone, no Kelly."""
    size = cfg["whale"]["size"]
    lo, hi = d(size["min_stake_usd"]), d(size["max_stake_usd"])
    x = (d(S) - d(cfg["whale"]["min_score_lb"])) / d(size["cap_points"])
    return q6(lo + min(max(x, Decimal(0)), Decimal(1)) * (hi - lo))


def kelly_stake(S, price, cfg) -> Decimal:
    """§8 amendment 2026-09-05: `kelly_fraction x e / (1 - p)` in dollars of the
    $1 paper unit, clipped to [min_stake_usd, max_stake_usd].

    No edge (`e <= 0`) books the FLOOR rather than nothing, because every fired
    signal books all three rows and a missing row would silently change what
    the sizings are compared over. A price at or above 1 cannot come off an ask
    and an absent one cannot come off a fired signal; both read as no reading
    and take the floor too, never a division that explodes the stake."""
    size = cfg["whale"]["size"]
    lo, hi = d(size["min_stake_usd"]), d(size["max_stake_usd"])
    e = d(S) - d(cfg["whale"]["min_score_lb"])
    if e <= 0 or price is None or d(price) >= 1:
        return q6(lo)
    f = d(size["kelly_fraction"]) * e / (1 - d(price))
    return q6(min(max(f, lo), hi))


def stake_for(sizing, S, price, cfg) -> Decimal:
    """The booked `stake_usd` for one (fired signal, sizing). `price` is the
    touch ask the candidate books at — read by `kelly` alone."""
    if sizing == "flat":
        return Decimal(1)
    if sizing == "conf":
        return conf_stake(S, cfg)
    return kelly_stake(S, price, cfg)
