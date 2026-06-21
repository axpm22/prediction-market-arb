from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from arb.forecast.scoring import brier, log_loss, summarize
from arb.pricing import q6
from arb.store import Store


def log_forecast(
    store: Store,
    *,
    vertical: str,
    category: str,
    market: str,
    p_you: Decimal,
    p_market: Decimal,
    p_base: Decimal | None = None,
    executable_price: Decimal | None = None,
    settle_by: str | None = None,
    edge_min: Decimal | None = None,
    record_paper_bet: bool = False,
) -> int:
    _validate_probability(p_you, "p_you")
    _validate_probability(p_market, "p_market")
    if p_base is not None:
        _validate_probability(p_base, "p_base")
    if executable_price is not None:
        _validate_probability(executable_price, "executable_price")
    now = datetime.now(timezone.utc).isoformat()
    with store.connect() as con:
        cur = con.execute(
            """INSERT INTO forecasts(made_at, vertical, category, market, p_base, p_you, p_market,
                       executable_price, settle_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                vertical,
                category,
                market,
                None if p_base is None else str(p_base),
                str(p_you),
                str(p_market),
                None if executable_price is None else str(executable_price),
                settle_by,
            ),
        )
        forecast_id = int(cur.lastrowid)
    if record_paper_bet and edge_min is not None and abs(p_you - p_market) >= edge_min:
        _record_forecast_bet(store, forecast_id, vertical, category, market, p_you, p_market, executable_price)
    return forecast_id


def settle_forecast(
    store: Store,
    forecast_id: int,
    outcome: int,
    *,
    closing_price: Decimal | None = None,
    clip: float = 0.000001,
) -> None:
    if outcome not in (0, 1):
        raise ValueError("outcome must be 0 or 1")
    with store.connect() as con:
        row = con.execute("SELECT * FROM forecasts WHERE id = ?", (forecast_id,)).fetchone()
        if row is None:
            raise ValueError(f"forecast {forecast_id} not found")
        clv = None
        if closing_price is not None and row["executable_price"] is not None:
            clv = float(q6(closing_price - Decimal(str(row["executable_price"]))))
        con.execute(
            """UPDATE forecasts
                  SET settled_at = ?, outcome = ?, brier_you = ?, brier_market = ?,
                      logloss_you = ?, logloss_market = ?, clv = ?
                WHERE id = ?""",
            (
                datetime.now(timezone.utc).isoformat(),
                outcome,
                brier(row["p_you"], outcome),
                brier(row["p_market"], outcome),
                log_loss(row["p_you"], outcome, clip),
                log_loss(row["p_market"], outcome, clip),
                clv,
                forecast_id,
            ),
        )


def report_data(store: Store, bins: int = 10, clip: float = 0.000001) -> dict:
    with store.connect() as con:
        rows = [
            dict(row)
            for row in con.execute(
                """SELECT * FROM forecasts
                    WHERE outcome IS NOT NULL
                    ORDER BY made_at, id"""
            )
        ]
    summary, calibration = summarize(rows, bins=bins, clip=clip)
    clv_values = [float(row["clv"]) for row in rows if row["clv"] is not None]
    mean_clv = round(sum(clv_values) / len(clv_values), 6) if clv_values else None
    return {
        "summary": summary,
        "calibration": calibration,
        "mean_clv": mean_clv,
        "clv_n": len(clv_values),
    }


def format_report(data: dict, min_n_for_claim: int) -> str:
    summary = data["summary"]
    lines = [
        "Forecast Report",
        f"Settled forecasts: {summary.n}",
        f"Brier you={summary.mean_brier_you:.6f} market={summary.mean_brier_market:.6f} skill={summary.brier_skill:.6f}",
        f"Log loss you={summary.mean_logloss_you:.6f} market={summary.mean_logloss_market:.6f} skill={summary.logloss_skill:.6f}",
        f"ECE={summary.ece:.6f}",
    ]
    if summary.brier_skill_ci95 is None:
        lines.append("Brier skill CI: insufficient settled forecasts (need >=30).")
    else:
        lo, hi = summary.brier_skill_ci95
        lines.append(f"Brier skill CI95=[{lo:.6f}, {hi:.6f}]")
    if data["mean_clv"] is not None:
        lines.append(f"Mean CLV={data['mean_clv']:.6f} over {data['clv_n']} forecasts")
    if summary.n < min_n_for_claim:
        lines.append(f"Claim status: insufficient sample to claim out-performance (need >= {min_n_for_claim}).")
    else:
        lines.append("Claim status: check pre-registered CI and skill threshold before claiming out-performance.")
    lines.append("")
    lines.append("Calibration")
    lines.append("bin,n,predicted,realized,abs_error")
    for row in data["calibration"]:
        lines.append(
            f"{row['bin']},{row['n']},{_empty(row['predicted'])},{_empty(row['realized'])},{_empty(row['abs_error'])}"
        )
    return "\n".join(lines)


def _record_forecast_bet(
    store: Store,
    forecast_id: int,
    vertical: str,
    category: str,
    market: str,
    p_you: Decimal,
    p_market: Decimal,
    executable_price: Decimal | None,
) -> None:
    side = "YES" if p_you > p_market else "NO"
    edge = abs(p_you - p_market)
    leg = {
        "venue": "forecast",
        "book": market,
        "forecast_id": str(forecast_id),
        "outcome_label": side,
        "p_you": str(p_you),
        "p_market": str(p_market),
    }
    with store.connect() as con:
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, stake_usd, legs_json, optimistic_cost, status, placed_at, notes)
               VALUES (?, 0, ?, ?, 'FORECAST', 0, 0, ?, '1.00', ?, ?, 'OPEN', ?, ?)""",
            (
                f"forecast:{forecast_id}",
                category,
                vertical,
                str(q6(edge)),
                json.dumps([leg]),
                None if executable_price is None else str(executable_price),
                datetime.now(timezone.utc).isoformat(),
                f"forecast-id={forecast_id} | side={side} | model-vs-market={q6(edge)}",
            ),
        )


def _validate_probability(value: Decimal, name: str) -> None:
    if value < 0 or value > 1:
        raise ValueError(f"{name} must be between 0 and 1")


def _empty(value) -> str:
    return "" if value is None else str(value)
