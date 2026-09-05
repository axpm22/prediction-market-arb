"""L14 whale scoring: prints -> positions -> realized edge -> shrunk lower bound.

PURE. No sqlite, no network, no clock: callers hand in plain rows and a
required `as_of`, which is what makes the frozen-clock proof possible (spec
§6 "Point-in-time"). Hygiene lives in arb/whale_hygiene.py, on size alone.

Rule 4: every threshold comes from `cfg["whale"]`. Rule 5: prices, sizes and
edges are Decimal; the normal-normal chain runs in float (a statistical
internal) and every Score field is re-quantized to 6 places with
arb.models.q6. Root isolation is `_agg_keys`' job — see there.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.models import q6
# The nonsports fee categories ARE the nonsports root: one list, kept in
# fees.py, so a new category cannot become a "sport" by omission.
from arb.venues.fees import _PM_CATEGORY_RATE_KEY

NONSPORTS = frozenset(_PM_CATEGORY_RATE_KEY) - {"sports"}
LEVELS = ("root", "sport", "bucket")
BUY, SELL = "BUY", "SELL"


@dataclass(frozen=True)
class Position:
    """One wallet's BUY window in one (condition_id, outcome_index). `entry_ts`
    is the first BUY, `last_ts` the last (spec §6's "position's print", what
    pregame and the §7 signal window test). `sold_size`/`sell_value` are the
    FIFO-matched SELLs against it."""
    wallet: str
    condition_id: str
    outcome_index: int
    avg_price: Decimal
    size: Decimal
    notional_usd: Decimal
    entry_ts: int
    last_ts: int
    pregame: bool
    sport: str
    market_type: str
    root: str
    source: str
    sold_size: Decimal = Decimal(0)
    sell_value: Decimal = Decimal(0)

    @property
    def bucket(self) -> str:
        return f"{self.sport}:{self.market_type}"


@dataclass(frozen=True)
class Outcome:
    """A resolved, scorable position: its realized edge and when it landed.
    `avg_price` rides along for arb/whale_bands.py — the one field that says
    which PRICE BAND the edge is graded against."""
    wallet: str
    root: str
    sport: str
    bucket: str
    e: Decimal
    resolved_at: datetime
    avg_price: Decimal


@dataclass(frozen=True)
class Score:
    n_bucket: int
    n_sport: int
    n_root: int
    mu: Decimal
    sd: Decimal
    lb: Decimal
    lb_report: Decimal
    native: bool


def root_of(sport, market_type) -> str | None:
    """None means unmappable — such a market never forms a scored position.
    The root IS the Polymarket fee category (2026-09-04), so `politics` and
    `crypto` are DISJOINT roots rather than one pooled `nonsports` one; because
    `_agg_keys` already carries `root` in position 1 of every key, isolating
    the score, the fee and the report by root is structural, not a filter."""
    if not sport:
        return None
    if sport in NONSPORTS:
        return sport
    return market_type if market_type in NONSPORTS else "sports"


def _epoch(iso) -> int | None:
    if not iso:
        return None
    t = datetime.fromisoformat(str(iso))
    return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())


def _when(iso) -> datetime | None:
    if not iso:
        return None
    t = datetime.fromisoformat(str(iso))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ positions

def positions_from_prints(prints, markets, cfg, res=None) -> list[Position]:
    """BUY windows per (wallet, condition_id, outcome_index); SELLs reduce the
    wallet's open positions FIFO and never form one (Polymarket has no native
    short). `res` is an optional diagnostics dict — dust, unmapped, orphan_sell.

    posperf (2026-09-04): a group's rows must land in ascending ts, but the
    whole tape need not — the caller already asks SQLite for that order
    (idx_prints_score, arb/whale_tape.py), so this only re-sorts a group whose
    own ts sequence goes backwards. `notional_usd` is parsed to Decimal once
    per row, before price/size, so a dust row costs one Decimal() call."""
    w = cfg["whale"]
    window, min_usd = int(w["position_window_secs"]), Decimal(str(w["min_print_usd"]))
    res = {} if res is None else res
    groups = defaultdict(list)
    last_ts = {}
    unordered = set()
    for p in prints:
        if p["outcome_index"] is None:
            # a venue row with no `outcomeIndex` names no side, so no resolution
            # can ever match it — `winner_outcome_index == None` is false for
            # every winner, which would have scored it as a LOSS.
            res["no_outcome_index"] = res.get("no_outcome_index", 0) + 1
            continue
        key = (p["proxy_wallet"], p["condition_id"], p["outcome_index"])
        ts = int(p["ts"])
        if key in last_ts and ts < last_ts[key]:
            unordered.add(key)
        last_ts[key] = ts
        groups[key].append(p)
    for key in unordered:
        groups[key].sort(key=lambda r: int(r["ts"]))

    out = []
    for (wallet, cid, idx), rows in groups.items():
        mk = markets.get(cid) or {}
        root = root_of(mk.get("sport"), mk.get("market_type"))
        if root is None:
            res["unmapped"] = res.get("unmapped", 0) + len(rows)
            continue
        start = _epoch(mk.get("start_utc"))
        opens, cur = [], None
        for p in rows:
            # notional first and alone: a dust row never needs price/size/ts
            # parsed at all, and every row that clears the floor still parses
            # notional only once (was: once here, again into cur["notional"]).
            notional = Decimal(str(p["notional_usd"]))
            if notional < min_usd:
                res["dust"] = res.get("dust", 0) + 1
                continue
            price, size = Decimal(str(p["price"])), Decimal(str(p["size"]))
            ts = int(p["ts"])
            if p["side"] == SELL:
                left = size
                for o in opens:                       # oldest first
                    take = min(left, o["size"] - o["sold"])
                    if take > 0:
                        o["sold"] += take
                        o["value"] += take * price
                        left -= take
                    if left <= 0:
                        break
                if left >= size:
                    res["orphan_sell"] = res.get("orphan_sell", 0) + 1
                continue
            if cur is None or ts - cur["last"] > window:
                # source is the window's FIRST print: a position that started as
                # backfill stays backfill for the report's with/without split.
                cur = {"first": ts, "last": ts, "size": Decimal(0), "cost": Decimal(0),
                       "notional": Decimal(0), "sold": Decimal(0), "value": Decimal(0),
                       "source": p.get("source", "tape")}
                opens.append(cur)
            cur["last"] = ts
            cur["size"] += size
            cur["cost"] += price * size
            cur["notional"] += notional
        for o in opens:
            out.append(Position(
                wallet=wallet, condition_id=cid, outcome_index=idx,
                avg_price=q6(o["cost"] / o["size"]), size=o["size"],
                notional_usd=q6(o["notional"]), entry_ts=o["first"], last_ts=o["last"],
                pregame=start is not None and o["last"] < start,
                sport=mk.get("sport"), market_type=mk.get("market_type"), root=root,
                source=o["source"], sold_size=o["sold"], sell_value=q6(o["value"])))
    return out


def realized_edge(pos: Position, payout: Decimal) -> Decimal:
    """Spec §6: shares sold before resolution realize their sell price, the
    rest the payout — per share, so it is comparable to a price."""
    got = pos.sell_value + (pos.size - pos.sold_size) * Decimal(payout)
    return q6(got / pos.size - pos.avg_price)


# ------------------------------------------------------------------- outcomes

def resolved_outcomes(positions, resolutions, as_of) -> list[Outcome]:
    """Scorable positions only: pregame, market RESOLVED (never void), and
    `resolved_at < as_of`. `as_of` is required — that is the whole point."""
    out = []
    for p in positions:
        r = resolutions.get(p.condition_id)
        if not p.pregame or not r or r.get("status") != "resolved":
            continue
        when = _when(r.get("resolved_at"))
        if when is None or when >= as_of:
            continue
        payout = Decimal(1) if r.get("winner_outcome_index") == p.outcome_index else Decimal(0)
        out.append(Outcome(wallet=p.wallet, root=p.root, sport=p.sport,
                           bucket=p.bucket, e=realized_edge(p, payout),
                           resolved_at=when, avg_price=p.avg_price))
    return out


def _agg_keys(o: Outcome) -> tuple:
    """The ONE place an outcome becomes an aggregation key. Every key carries
    `root` in position 1, so no sum can ever cross the sports/nonsports line."""
    return ((o.wallet, o.root),
            (o.wallet, o.root, o.sport),
            (o.wallet, o.root, o.sport, o.bucket))


def aggregate(outcomes) -> dict[tuple, tuple[int, Decimal]]:
    """key -> (n, sum of e). Decimal sums, so the total is order-independent
    and the frozen-clock comparison is bit-for-bit."""
    agg = defaultdict(lambda: [0, Decimal(0)])
    for o in outcomes:
        for key in _agg_keys(o):
            cell = agg[key]
            cell[0] += 1
            cell[1] += o.e
    return {k: (n, s) for k, (n, s) in agg.items()}


def base_rate(outcomes, root, as_of, cfg) -> tuple[Decimal, int]:
    """`m0`: mean realized edge of EVERY wallet in `root` over the last
    `base_rate_lookback_days`. Below `base_rate_min_n` the default stands. The
    assembly (`whale_state.score_state`) calls this on BAND-ADJUSTED edges
    (arb/whale_bands.py) — the same statistic of a re-centred population, so
    m0 stays the EB prior mean, now centred near zero."""
    s = cfg["whale"]["score"]
    floor = as_of - timedelta(days=int(s["base_rate_lookback_days"]))
    es = [o.e for o in outcomes if o.root == root and floor <= o.resolved_at < as_of]
    if len(es) < int(s["base_rate_min_n"]):
        return q6(Decimal(s["base_rate_default"])), len(es)
    return q6(sum(es) / len(es)), len(es)


# --------------------------------------------------------------------- model

def posterior(cfg, m0, levels) -> tuple[float, float]:
    """Spec §6's exact two-level Gaussian chain, root -> sport -> bucket.
    `levels` is [(n, ebar)] per level. At n = 0 a level's variance is exactly
    `prior_sd² + var_parent`; as n -> inf it is `obs_var / n`."""
    s = cfg["whale"]["score"]
    obs_var = float(s["obs_var"])
    mu, var = float(m0), 0.0
    for name, (n, ebar) in zip(LEVELS, levels):
        k = obs_var / (float(s["prior_sd"][name]) ** 2 + var)
        mu = (n * ebar + k * mu) / (n + k)
        var = obs_var / (n + k)
    return mu, var


def _mean(cell) -> tuple[int, float]:
    n, total = cell
    return n, (float(total) / n if n else 0.0)


def score_for(agg, wallet, sport, market_type, cfg, base_rates) -> Score:
    """The score a wallet carries into `sport:market_type`, whether or not it
    has ever traded that bucket (n_bucket = 0 is the transfer case, §6)."""
    w = cfg["whale"]
    root = root_of(sport, market_type)
    empty = (0, Decimal(0))
    bucket = f"{sport}:{market_type}"
    nr, er = _mean(agg.get((wallet, root), empty))
    ns, es = _mean(agg.get((wallet, root, sport), empty))
    nb, eb = _mean(agg.get((wallet, root, sport, bucket), empty))
    m0 = base_rates.get(root, w["score"]["base_rate_default"])
    mu, var = posterior(cfg, m0, [(nr, er), (ns, es), (nb, eb)])
    sd = math.sqrt(var)
    # native: the same chain fed nothing but this bucket's own record (§7).
    own_mu, own_var = posterior(cfg, m0, [(nb, eb), (nb, eb), (nb, eb)])
    own_lb = q6(Decimal(own_mu - float(w["score"]["z"]) * math.sqrt(own_var)))
    return Score(n_bucket=nb, n_sport=ns, n_root=nr, mu=q6(Decimal(mu)),
                 sd=q6(Decimal(sd)),
                 lb=q6(Decimal(mu - float(w["score"]["z"]) * sd)),
                 lb_report=q6(Decimal(mu - float(w["score"]["z_report"]) * sd)),
                 native=own_lb >= Decimal(w["min_score_lb"]))
