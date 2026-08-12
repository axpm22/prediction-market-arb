"""B3 — BTC cross-venue oracle-divergence tracker (docs/MicroEVBitcoin.md).

Cross-venue BTC arbs are permanently REVIEW because Kalshi (CF Benchmarks BRTI) and
Polymarket (UMA) are not PROVEN to settle identically (hard rule #6). This turns that into an
empirical question: at each window end record both venues' settlement print and the realized
binary result, then measure how often they disagree. Zero disagreement over enough decided
windows is the evidence required to whitelist (kalshi,polymarket)+btc in config.yaml and
promote those flags from REVIEW to OK. Append-only; paper-only; public reads only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from arb.store import Store

# Minimum decided windows before a clean record is treated as whitelist evidence. Methodology
# knob (not a pricing threshold), so it stays a parameter rather than a config rate.
DEFAULT_MIN_WINDOWS = 30


@dataclass(frozen=True)
class OracleDivergence:
    n: int                     # windows recorded
    decided: int               # windows where BOTH venues returned a binary result
    disagreements: int         # decided windows where the binary results differ
    disagree_rate: float
    max_price_gap: float       # max |kalshi_print - poly_print| over windows with both prints
    mean_price_gap: float
    whitelist_eligible: bool   # decided >= min_windows AND zero disagreements


def analyze_oracle_divergence(records: list[dict[str, Any]],
                              min_windows: int = DEFAULT_MIN_WINDOWS) -> OracleDivergence:
    decided = 0
    disagreements = 0
    gaps: list[float] = []
    for r in records:
        kr, pr = r.get("kalshi_result"), r.get("poly_result")
        if kr is not None and pr is not None:
            decided += 1
            if int(kr) != int(pr):
                disagreements += 1
        kp, pp = r.get("kalshi_print"), r.get("poly_print")
        if kp not in (None, "") and pp not in (None, ""):
            gaps.append(abs(float(Decimal(str(kp)) - Decimal(str(pp)))))
    return OracleDivergence(
        n=len(records),
        decided=decided,
        disagreements=disagreements,
        disagree_rate=round(disagreements / decided, 4) if decided else 0.0,
        max_price_gap=round(max(gaps), 2) if gaps else 0.0,
        mean_price_gap=round(sum(gaps) / len(gaps), 2) if gaps else 0.0,
        whitelist_eligible=(decided >= min_windows and disagreements == 0),
    )


def record_btc_print(store: Store, asset: str, window_end_utc: datetime,
                     kalshi_print: Any = None, poly_print: Any = None,
                     kalshi_result: int | None = None, poly_result: int | None = None) -> None:
    """Append one window's settlement evidence. Append-only; re-recording a window adds a row
    (the analyzer is robust to it) rather than mutating — the ledger is never edited."""
    window = (window_end_utc.astimezone(timezone.utc).isoformat()
              if hasattr(window_end_utc, "astimezone") else str(window_end_utc))
    with store.connect() as con:
        con.execute(
            """INSERT INTO btc_oracle_prints(recorded_at, asset, window_end_utc,
                   kalshi_print, poly_print, kalshi_result, poly_result)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), asset, window,
             None if kalshi_print is None else str(kalshi_print),
             None if poly_print is None else str(poly_print),
             None if kalshi_result is None else int(kalshi_result),
             None if poly_result is None else int(poly_result)),
        )


def oracle_divergence(store: Store, min_windows: int = DEFAULT_MIN_WINDOWS) -> OracleDivergence:
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT kalshi_print, poly_print, kalshi_result, poly_result FROM btc_oracle_prints")]
    return analyze_oracle_divergence(rows, min_windows)


def format_divergence(d: OracleDivergence, min_windows: int = DEFAULT_MIN_WINDOWS) -> str:
    verdict = (
        "WHITELIST-ELIGIBLE — 0 disagreements over enough decided windows; the cross-venue BTC "
        "pair can be promoted REVIEW -> OK in config.trusted_pairs."
        if d.whitelist_eligible else
        f"NOT yet — need >= {min_windows} decided windows with 0 disagreements "
        f"(have {d.decided} decided, {d.disagreements} disagreements)."
    )
    return (
        f"BTC oracle divergence: {d.n} windows ({d.decided} decided), "
        f"{d.disagreements} disagreements ({d.disagree_rate:.1%}); "
        f"price gap max ${d.max_price_gap} / mean ${d.mean_price_gap}\n  {verdict}"
    )
