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
from datetime import datetime, timezone
from decimal import Decimal
import json

from arb.pricing import q6
from arb.store import Store


@dataclass(frozen=True)
class WalletStat:
    address: str
    n_trades: int
    win_rate: float
    top_category: str
    top_category_share: float
    qualified: bool
    suspect: bool         # win rate above ceiling -> likely zombie-order illusion
    pnl_usd: Decimal = Decimal("0")
    roi: float = 0.0      # realized P&L / cost on settled trades — the profitability gate


def wallet_stats(trades: list[dict], settled_results: dict[str, bool], categories: dict[str, str],
                 cfg: dict) -> list[WalletStat]:
    """Aggregate trades per wallet and apply the qualification filters.

    settled_results: conditionId -> did the wallet's side win (for settled markets only).
    categories: conditionId -> category label (for concentration).
    """
    min_trades = int(cfg.get("min_trades", 100))
    wr_ceiling = float(cfg.get("wr_ceiling", 0.72))
    min_share = float(cfg.get("min_category_share", 0.50))
    min_roi = float(cfg.get("min_roi", 0.0))  # pass3#10: profitability gate, not hit rate

    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_wallet[str(t["proxyWallet"])].append(t)

    out: list[WalletStat] = []
    for addr, ts in by_wallet.items():
        n = len(ts)
        settled = [t for t in ts if str(t["conditionId"]) in settled_results]
        wins = sum(1 for t in settled if settled_results[str(t["conditionId"])])
        win_rate = wins / len(settled) if settled else 0.0
        # pass3#10: realized P&L and ROI on settled trades. A share costs `price`*`size` and
        # pays `size` if it wins; the module's own thesis is that a high win rate can be
        # right-but-unprofitable (hedge legs, zombie-order inflation), so qualify on ROI.
        cost = Decimal("0")
        pnl = Decimal("0")
        for t in settled:
            price = Decimal(str(t.get("price", 0)))
            size = Decimal(str(t.get("size", 0)))
            stake = price * size
            cost += stake
            pnl += (size - stake) if settled_results[str(t["conditionId"])] else -stake
        roi = float(pnl / cost) if cost > 0 else 0.0
        cat_counts: dict[str, int] = defaultdict(int)
        for t in ts:
            cat_counts[categories.get(str(t["conditionId"]), "unknown")] += 1
        top_category = max(cat_counts, key=cat_counts.get) if cat_counts else "unknown"
        top_share = cat_counts[top_category] / n if n else 0.0
        suspect = win_rate > wr_ceiling
        qualified = (
            n >= min_trades
            and roi >= min_roi
            and top_share >= min_share
        )
        out.append(
            WalletStat(
                address=addr,
                n_trades=n,
                win_rate=round(win_rate, 4),
                top_category=top_category,
                top_category_share=round(top_share, 4),
                qualified=qualified,
                suspect=suspect,
                pnl_usd=pnl,
                roi=round(roi, 4),
            )
        )
    return out


def persist_wallet_stats(store: Store, stats: list[WalletStat]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.connect() as con:
        con.executemany(
            """INSERT INTO wallets(address, first_seen, n_trades, win_rate, pnl_usd,
                       top_category, category_share, qualified, suspect)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(address) DO UPDATE SET
                   n_trades = excluded.n_trades,
                   win_rate = excluded.win_rate,
                   pnl_usd = excluded.pnl_usd,
                   top_category = excluded.top_category,
                   category_share = excluded.category_share,
                   qualified = excluded.qualified,
                   suspect = excluded.suspect""",
            [
                (
                    stat.address,
                    now,
                    stat.n_trades,
                    stat.win_rate,
                    str(stat.pnl_usd),
                    stat.top_category,
                    stat.top_category_share,
                    int(stat.qualified),
                    int(stat.suspect),
                )
                for stat in stats
            ],
        )


def record_wallet_trades(store: Store, trades: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for trade in trades:
        rows.append(
            (
                str(trade["proxyWallet"]),
                str(trade.get("observed_at") or trade.get("timestamp") or now),
                str(trade["conditionId"]),
                str(trade.get("side", "")),
                str(Decimal(str(trade.get("price", "0")))),
                str(Decimal(str(trade.get("size", trade.get("size_usd", "0"))))),
                int(bool(trade.get("settled", False))),
                None if trade.get("result") is None else str(trade["result"]),
            )
        )
    with store.connect() as con:
        con.executemany(
            """INSERT INTO wallet_trades(address, observed_at, market, side, price, size_usd, settled, result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


def record_copy_bet(
    store: Store,
    poll_id: int,
    *,
    address: str,
    condition_id: str,
    category: str,
    side: str,
    whale_price: Decimal,
    next_executable_price: Decimal,
    size_usd: Decimal,
    market_title: str = "",
    suspect: bool = False,
) -> int:
    """Log the honest copy experiment: whale price optimistic, post-move fill confirmed."""
    slippage = copy_fill_slippage(whale_price, next_executable_price)
    fingerprint = (
        f"copy:{address}:{condition_id}:{side}:{whale_price}:"
        f"{next_executable_price}:{size_usd}"
    )
    leg = {
        "venue": "polymarket",
        "book": condition_id,
        "wallet": address,
        "category": category,
        "outcome_label": side,
        "whale_price": str(whale_price),
        "confirmed_price": str(next_executable_price),
        "slippage": str(slippage),
    }
    with store.connect() as con:
        if con.execute(
            "SELECT 1 FROM flat_bets WHERE fingerprint = ? AND status = 'OPEN' LIMIT 1",
            (fingerprint,),
        ).fetchone():
            return 0
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, stake_usd, legs_json, optimistic_cost, confirmed_cost, status, placed_at,
                   notes, quality_flag)
               VALUES (?, ?, 'nonsports', ?, 'COPY', 0, 0, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
            (
                fingerprint,
                poll_id,
                category,
                str(q6(-slippage)),
                str(size_usd),
                json.dumps([leg]),
                str(whale_price),
                str(next_executable_price),
                datetime.now(timezone.utc).isoformat(),
                f"wallet={address} | slippage={slippage} | {market_title[:80]}",
                "suspect-wallet" if suspect else None,
            ),
        )
    return 1


def copy_fill_slippage(whale_price: Decimal, next_executable_price: Decimal) -> Decimal:
    """Adverse-selection cost of copying after the move: what you pay minus the whale's
    price. Positive = you paid more than the whale (the signal moved against you)."""
    return (next_executable_price - whale_price).quantize(Decimal("0.000001"))


def is_uncopyable_category(category: str, skip_categories: list[str]) -> bool:
    """BTC/ETH 15-min bot wallets are spread-capture; copying eats the edge."""
    return category in set(skip_categories)


DATA_API = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": "arb-paper-scanner/0.1 (public read-only)"}


def fetch_recent_trades(limit: int = 500) -> list[dict]:
    """Public, keyless Polymarket trade feed (on-chain). Fixtures-first verified."""
    import requests

    r = requests.get(f"{DATA_API}/trades", params={"limit": limit}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def run_whale_pass(store: Store, cfg: dict, trades: list[dict] | None = None) -> tuple[int, int]:
    """Ingest trades, compute + persist wallet stats and the trades. Returns (wallets, qualified).

    Settlement/category enrichment for win-rate is a later step (needs per-condition
    resolution); this pass establishes the wallet universe and trade history so the
    qualification filters and copy experiment have data to run on.
    """
    import requests

    try:
        trades = trades if trades is not None else fetch_recent_trades()
    except requests.RequestException as exc:
        raise RuntimeError(f"whale trade fetch failed: {exc}") from exc
    # settled_results/categories are empty here (enrichment is a separate pass); the
    # universe + trade log still get persisted so the cohort can be built up over time.
    stats = wallet_stats(trades, settled_results={}, categories={}, cfg=cfg)
    persist_wallet_stats(store, stats)
    record_wallet_trades(store, trades)
    qualified = sum(1 for s in stats if s.qualified)
    return len(stats), qualified
