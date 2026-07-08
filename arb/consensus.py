"""Consensus computation (SPEC §7.2): median de-vigged p across >=3 books
(totals: 2, tagged thin-consensus). Per-book de-vig persists to consensus_books
so the n>=200 bake-off re-run is runnable from stored data (v3.3)."""
from __future__ import annotations

import statistics
from decimal import Decimal

from arb.models import q6
from arb.pricing import devig


def compute_consensus(cfg, rows: list[dict], computed_at: str
                      ) -> tuple[list[dict], list[dict]]:
    """rows: oddsapi.parse_events output. -> (consensus rows, consensus_books rows)."""
    method = cfg["devig_method"]
    by_market: dict[tuple, dict[str, list[dict]]] = {}
    for r in rows:
        key = (r["home"], r["away"], r["market"], r["line"])
        by_market.setdefault(key, {}).setdefault(r["book"], []).append(r)

    book_rows, cons_rows = [], []
    for (home, away, market, line), books in by_market.items():
        per_book: dict[tuple, list] = {}  # outcome -> [(book, p_devig)]
        for book, outs in books.items():
            if len(outs) < 2:
                continue  # cannot de-vig a one-sided listing
            probs = devig([o["odds"] for o in outs], method)
            for o, p in zip(outs, probs):
                per_book.setdefault(o["outcome"], []).append((book, o["odds"], p))
                book_rows.append({
                    "event_key": f"{home}|{away}", "market_type":
                        "TOTAL" if market == "totals" else "WINNER",
                    "line": line, "outcome": o["outcome"], "book": book,
                    "raw_odds": o["odds"], "p_devig": p, "method": method,
                    "computed_at": computed_at})
        floor = int(cfg["min_books_for_consensus_totals"] if market == "totals"
                    else cfg["min_books_for_consensus"])
        for outcome, entries in per_book.items():
            ps = [p for _, _, p in entries]
            if len(ps) < floor:
                continue
            # outlier drop disabled below 3 books (v3.3)
            if len(ps) >= 3:
                med = statistics.median(ps)
                kept = [p for p in ps if abs(p - med) <= cfg["consensus_outlier_points"]]
                ps = kept or ps
            med = statistics.median(ps)
            qs = sorted(ps)
            iqr = qs[int(0.75 * (len(qs) - 1))] - qs[int(0.25 * (len(qs) - 1))] \
                if len(qs) > 1 else Decimal(0)
            cons_rows.append({
                "event_key": f"{home}|{away}", "market_type":
                    "TOTAL" if market == "totals" else "WINNER",
                "line": line, "outcome": outcome, "p_hat": q6(Decimal(med)),
                "method": method if len(ps) > 2 else f"{method};thin-consensus",
                "n_books": len(ps), "dispersion": q6(Decimal(iqr)),
                "computed_at": computed_at})
    return cons_rows, book_rows
