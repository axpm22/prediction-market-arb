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
        _settle_forecast_bet(con, forecast_id, outcome)


def _settle_forecast_bet(con, forecast_id: int, outcome: int) -> None:
    # #4 Realize the linked paper bet so FORECAST rows don't sit OPEN forever. flat_bets is
    # append-only (hard rule #9), so append a SETTLED row referencing the OPEN bet.
    base = con.execute(
        """SELECT * FROM flat_bets b
            WHERE b.fingerprint = ? AND b.status = 'OPEN' AND b.kind = 'FORECAST'
              AND NOT EXISTS (SELECT 1 FROM flat_bets s
                               WHERE s.status = 'SETTLED' AND s.reappearance_of = b.id)
            ORDER BY b.id LIMIT 1""",
        (f"forecast:{forecast_id}",),
    ).fetchone()
    if base is None:
        return
    legs = json.loads(base["legs_json"]) if base["legs_json"] else [{}]
    side = legs[0].get("outcome_label", "YES")
    won = (side == "YES" and outcome == 1) or (side == "NO" and outcome == 0)
    result = "WON" if won else "LOST"
    cost = Decimal(str(base["optimistic_cost"]))
    stake = Decimal(str(base["stake_usd"]))
    pnl = q6(stake * (Decimal("1") / cost - Decimal("1"))) if won else q6(-stake)
    con.execute(
        """INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind,
               trusted, live, edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic,
               status, placed_at, settled_at, result, notes)
           VALUES (?, ?, ?, ?, ?, 'FORECAST', ?, 0, ?, ?, ?, ?, ?, 'SETTLED', ?, ?, ?, ?)""",
        (
            base["fingerprint"], base["id"], base["opened_poll_id"], base["category"], base["sport"],
            base["trusted"], base["edge"], base["stake_usd"], base["legs_json"],
            base["optimistic_cost"], str(pnl),
            datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
            result, f"forecast-settle id={forecast_id} outcome={outcome}",
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
        f"Brier paired diff (you-market)={summary.mean_brier_diff:.6f} (negative = you better)",
        f"Log loss you={summary.mean_logloss_you:.6f} market={summary.mean_logloss_market:.6f} skill={summary.logloss_skill:.6f}",
        f"ECE={summary.ece:.6f}",
    ]
    if summary.brier_diff_ci95 is None:
        lines.append("Brier paired-diff CI: insufficient settled forecasts (need >=30).")
    else:
        lo, hi = summary.brier_diff_ci95
        verdict = "you out-predict market" if hi < 0 else ("market out-predicts you" if lo > 0 else "no significant difference")
        lines.append(f"Brier paired-diff CI95=[{lo:.6f}, {hi:.6f}] -> {verdict}")
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
    # #10 No executable price => the bet can never produce P&L, so don't write a dead OPEN
    # row to the ledger; the calibration record still lives in the `forecasts` table.
    if executable_price is None or executable_price <= 0:
        return
    side = "YES" if p_you > p_market else "NO"
    disagreement = abs(p_you - p_market)
    # #10 Store an edge COMPARABLE to EV/ARB (return-per-dollar at the executable price),
    # not the model-vs-market disagreement (a different quantity that would corrupt any
    # cross-kind edge summation). The disagreement is kept in the notes.
    win_prob = p_you if side == "YES" else (Decimal("1") - p_you)
    edge = q6((win_prob - executable_price) / executable_price)
    leg = {
        "venue": "forecast",
        "book": market,
        "forecast_id": str(forecast_id),
        "outcome_label": side,
        "cost": str(executable_price),
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
                str(edge),
                json.dumps([leg]),
                str(executable_price),
                datetime.now(timezone.utc).isoformat(),
                f"forecast-id={forecast_id} | side={side} | model-vs-market={q6(disagreement)}",
            ),
        )


def _validate_probability(value: Decimal, name: str) -> None:
    if value < 0 or value > 1:
        raise ValueError(f"{name} must be between 0 and 1")


def _empty(value) -> str:
    return "" if value is None else str(value)
