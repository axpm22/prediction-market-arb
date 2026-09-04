"""L14 hygiene filters (spec §13). Split out of arb/whale_score.py on size
alone — same rules: pure, config-driven (rule 4), Decimal money (rule 5).

Two scopes, because the spec has two: `mm` and `wash` condemn the WALLET,
`both_sides` condemns only that wallet's positions in that ONE market. `dust`
is not here: it is a print filter and lives in positions_from_prints.

Flags are computed from raw PRINTS, not positions, on purpose: a quote-stuffing
maker's prints are all below min_print_usd, so by the time they were positions
the evidence would be gone.
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


def _pairs(rows, gap):
    """Every pair of rows within `gap` seconds. O(n^2) per market/wallet.
    # ponytail: a wallet's prints in one market are tens, not thousands; if a
    # backfilled whale ever makes this hurt, sort by ts and use a sliding window.
    """
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if abs(int(b["ts"]) - int(a["ts"])) <= gap:
                yield a, b


def hygiene_flags(prints, positions, cfg) -> Hygiene:
    h = cfg["whale"]["hygiene"]
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
            any(a["outcome_index"] != b["outcome_index"]
                for a, b in _pairs([p for p in mrows if p["side"] == BUY],
                                   int(h["mm_window_secs"])))
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
        if any(a["side"] != b["side"] and Decimal(str(a["price"])) == Decimal(str(b["price"]))
               for trows in by_token.values()
               for a, b in _pairs(trows, int(h["wash_gap_secs"]))):
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
