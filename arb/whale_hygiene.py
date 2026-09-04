"""L14 hygiene filters (spec §13). Split out of arb/whale_score.py on size
alone — same rules: pure, config-driven (rule 4), Decimal money (rule 5).

Two scopes, because the spec has two: `mm` and `wash` condemn the WALLET,
`both_sides` condemns only that wallet's positions in that ONE market. `dust`
is not here: it is a print filter and lives in positions_from_prints.

Flags are computed from raw PRINTS, not positions, on purpose: a quote-stuffing
maker's prints are all below min_print_usd, so by the time they were positions
the evidence would be gone.

Perf (regrade 2026-09-04, review of 7c913bf Important #2): the mm/wash scans
used to be a naive O(n^2) `_pairs` generator per (wallet, market)/(wallet,
token) group — the dominant cost of whale_state.score_state, which the 60s
whale cycle rebuilds every minute. Replaced with a sort + sliding-window scan
(`_two_sided_within_window`, `_wash_pair_within_window`): O(n log n) per
group, same flags bit-for-bit (tests/test_whale_score.py property-tests this
against the old O(n^2) implementation, kept there as a frozen reference).
Measured on a synthetic concentrated tape (bench_hygiene.py, see the
retention report), whale_state.score_state before -> after:
  50k rows:  0.83s   -> 0.41s
  200k rows: 10.69s  -> 1.93s
  500k rows: 66.93s  -> 4.90s
  1M rows:   385.53s -> 9.04s
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

BUY = "BUY"


@dataclass(frozen=True)
class Hygiene:
    """`wallets`: wallet -> {"mm", "wash"}. `markets`: (wallet, condition_id)
    -> {"both_sides"}. `excludes(position)` is what callers actually want."""
    wallets: dict[str, set[str]]
    markets: dict[tuple[str, str], set[str]]

    def excludes(self, pos) -> bool:
        return bool(self.wallets.get(pos.wallet)
                    or self.markets.get((pos.wallet, pos.condition_id)))


def _two_sided_within_window(rows, gap) -> bool:
    """True iff two rows with different `outcome_index` land within `gap`
    seconds of EACH OTHER — i.e. some pair, not just neighbors.

    O(n log n): sort by ts, then slide a two-pointer window [lo, hi]. Since
    rows are sorted, `ts[hi] - ts[lo] <= gap` makes EVERY pair inside the
    window mutually within `gap` (ts[hi] - ts[k] <= ts[hi] - ts[lo] for any
    lo <= k <= hi) — the same pairs the old nested-loop `_pairs` produced,
    just found without re-scanning the window on every row. A running count
    of outcome_index values currently in the window answers "two different
    values present" in O(1) instead of an O(window) rescan.
    """
    rows = sorted(rows, key=lambda r: int(r["ts"]))
    counts: dict = defaultdict(int)
    lo = 0
    for row in rows:
        ts_hi = int(row["ts"])
        while ts_hi - int(rows[lo]["ts"]) > gap:
            idx = rows[lo]["outcome_index"]
            counts[idx] -= 1
            if counts[idx] == 0:
                del counts[idx]
            lo += 1
        if any(idx != row["outcome_index"] for idx in counts):
            return True
        counts[row["outcome_index"]] += 1
    return False


def _wash_pair_within_window(rows, gap) -> bool:
    """True iff a BUY and a SELL at the SAME price land within `gap` seconds
    of each other. Same two-pointer window as `_two_sided_within_window`;
    the running state is price -> {side: count} in the window, so a match is
    an O(1) lookup instead of a rescan.
    """
    rows = sorted(rows, key=lambda r: int(r["ts"]))
    window: dict = defaultdict(lambda: defaultdict(int))
    lo = 0
    for row in rows:
        ts_hi = int(row["ts"])
        while ts_hi - int(rows[lo]["ts"]) > gap:
            lo_row = rows[lo]
            lo_price = Decimal(str(lo_row["price"]))
            window[lo_price][lo_row["side"]] -= 1
            lo += 1
        price = Decimal(str(row["price"]))
        sides = window.get(price)
        if sides and any(cnt > 0 for side, cnt in sides.items() if side != row["side"]):
            return True
        window[price][row["side"]] += 1
    return False


def hygiene_flags(prints, positions, cfg) -> Hygiene:
    h = cfg["whale"]["hygiene"]
    mm_window, wash_gap = int(h["mm_window_secs"]), int(h["wash_gap_secs"])
    by_wallet = defaultdict(list)
    for p in prints:
        by_wallet[p["proxy_wallet"]].append(p)

    wallets: dict[str, set[str]] = {}
    for wallet, rows in by_wallet.items():
        flags = set()
        by_market = defaultdict(list)
        for p in rows:
            by_market[p["condition_id"]].append(p)

        # mm: both outcomes bought inside mm_window_secs on a large enough
        # share of the wallet's markets, with tiny prints throughout.
        two_sided = sum(
            _two_sided_within_window([p for p in mrows if p["side"] == BUY], mm_window)
            for mrows in by_market.values())
        mean_usd = sum(Decimal(str(p["notional_usd"])) for p in rows) / len(rows)
        if (Decimal(two_sided) / len(by_market) >= Decimal(h["mm_market_share"])
                and mean_usd < Decimal(str(h["mm_max_avg_usd"]))):
            flags.add("mm")

        # wash: a BUY and a SELL of the same token, same price, seconds apart.
        # Under `taker_only: false` (2026-09-04) a wallet's maker legs are in
        # the feed too, so a maker quoting both sides of a token within
        # wash_gap_secs now trips this — correctly: that is a market maker, and
        # §13 wants it out of the signal either way.
        # Self-match note (2026-09-04, holdings-paging fix, review of a966232
        # Important #2): whether Polymarket's CLOB matches an order against
        # its own owner's resting order on the other side is NOT verified — no
        # fact row in data/verified_facts.md. If it does, the two legs share
        # one transactionHash/timestamp at one price with opposite sides,
        # which is exactly the pattern this predicate already flags. No code
        # change: that classification is correct either way, since a
        # self-match nets the wallet's exposure to zero same as any other wash.
        by_token = defaultdict(list)
        for p in rows:
            by_token[(p["condition_id"], p["outcome_index"])].append(p)
        if any(_wash_pair_within_window(trows, wash_gap) for trows in by_token.values()):
            flags.add("wash")
        wallets[wallet] = flags

    # both_sides: live exposure on both outcomes of one market beyond the
    # tolerance. Positions, not prints — a sold-down side is not exposure.
    exposure = defaultdict(lambda: defaultdict(Decimal))
    for pos in positions:
        exposure[(pos.wallet, pos.condition_id)][pos.outcome_index] += pos.size - pos.sold_size
    markets = {}
    for key, sides in exposure.items():
        live = [v for v in sides.values() if v > 0]
        if len(live) > 1 and min(live) > Decimal(h["both_sides_tolerance"]) * max(live):
            markets[key] = {"both_sides"}
    return Hygiene(wallets=wallets, markets=markets)
