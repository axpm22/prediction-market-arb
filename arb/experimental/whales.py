"""Polymarket whale-flow tracker — BUILD_PLAN Phase 5 (measurement / debunk study).

Blind whale copy-trading is mostly unprofitable (adverse selection, one-leg-of-a-hedge,
zombie-order win-rate inflation, bot spread-capture you can't replicate). Run as a
pre-registered experiment, "raw signal exists but slippage eats it" is the citable result.
Polymarket only — it's on-chain (data-api exposes per-wallet trades); Kalshi has no public
trader identities.

This module: ingest trades, compute per-wallet metrics, qualify wallets by the config
filters, and (for a qualifying wallet's new position) log a PAPER copy bet at the next
observable executable price — i.e. you fill AFTER the move, as a taker. The optimistic
(whale price) vs confirmed (your post-move fill) gap IS the adverse-selection cost.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class WalletStat:
    address: str
    n_trades: int
    win_rate: float
    top_category_share: float
    qualified: bool
    suspect: bool         # win rate above ceiling -> likely zombie-order illusion


def wallet_stats(trades: list[dict], settled_results: dict[str, bool], categories: dict[str, str],
                 cfg: dict) -> list[WalletStat]:
    """Aggregate trades per wallet and apply the qualification filters.

    settled_results: conditionId -> did the wallet's side win (for settled markets only).
    categories: conditionId -> category label (for concentration).
    """
    min_trades = int(cfg.get("min_trades", 100))
    wr_floor = float(cfg.get("wr_floor", 0.60))
    wr_ceiling = float(cfg.get("wr_ceiling", 0.72))
    min_share = float(cfg.get("min_category_share", 0.50))

    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_wallet[str(t["proxyWallet"])].append(t)

    out: list[WalletStat] = []
    for addr, ts in by_wallet.items():
        n = len(ts)
        settled = [t for t in ts if str(t["conditionId"]) in settled_results]
        wins = sum(1 for t in settled if settled_results[str(t["conditionId"])])
        win_rate = wins / len(settled) if settled else 0.0
        cat_counts: dict[str, int] = defaultdict(int)
        for t in ts:
            cat_counts[categories.get(str(t["conditionId"]), "unknown")] += 1
        top_share = max(cat_counts.values()) / n if n else 0.0
        suspect = win_rate > wr_ceiling
        qualified = (
            n >= min_trades
            and wr_floor <= win_rate <= wr_ceiling
            and top_share >= min_share
        )
        out.append(WalletStat(addr, n, round(win_rate, 4), round(top_share, 4), qualified, suspect))
    return out


def copy_fill_slippage(whale_price: Decimal, next_executable_price: Decimal) -> Decimal:
    """Adverse-selection cost of copying after the move: what you pay minus the whale's
    price. Positive = you paid more than the whale (the signal moved against you)."""
    return (next_executable_price - whale_price).quantize(Decimal("0.000001"))


def is_uncopyable_category(category: str, skip_categories: list[str]) -> bool:
    """BTC/ETH 15-min bot wallets are spread-capture; copying eats the edge."""
    return category in set(skip_categories)
