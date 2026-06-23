from decimal import Decimal
from pathlib import Path

from arb.bias import calibration
from arb.matching import load_aliases
from arb.quote_settlement import population_binaries, quote_outcomes
from arb.store import Store

FIX = Path("tests/fixtures")
EVENT = "baseball_mlb:2026-06-09:kansas city royals:texas rangers"


def _row(venue, book, outcome, cost, fetched_at):
    return {"event_key": EVENT, "venue": venue, "book": book, "outcome": outcome,
            "cost": cost, "fetched_at": fetched_at, "sport": "baseball_mlb"}


# KC beat TEX 5-3 in the fixture, so KC won.
SCORES = {"kansas city royals": Decimal("5"), "texas rangers": Decimal("3")}


def test_quote_outcomes_devigs_and_uses_closing() -> None:
    rows = [
        # two snapshots; the later one is the closing quote and must win the dedup.
        _row("oddsapi", "dk", "Kansas City Royals", "0.60", "2026-06-09T18:00:00+00:00"),
        _row("oddsapi", "dk", "Texas Rangers", "0.45", "2026-06-09T18:00:00+00:00"),
        _row("oddsapi", "dk", "Kansas City Royals", "0.55", "2026-06-09T23:30:00+00:00"),
        _row("oddsapi", "dk", "Texas Rangers", "0.50", "2026-06-09T23:30:00+00:00"),
        # one-sided book: can't de-vig, must be skipped.
        _row("kalshi", "KXMLB-KC", "Kansas City Royals", "0.52", "2026-06-09T23:30:00+00:00"),
    ]
    out = quote_outcomes(rows, lambda ek: SCORES, load_aliases())
    # de-vig the CLOSING pair 0.55/0.50 (sum 1.05): KC 0.5238 (won), TEX 0.4762 (lost).
    out_sorted = sorted(out)
    assert len(out) == 2
    assert abs(out_sorted[1][0] - 0.55 / 1.05) < 1e-6 and out_sorted[1][1] is True   # KC won
    assert abs(out_sorted[0][0] - 0.50 / 1.05) < 1e-6 and out_sorted[0][1] is False  # TEX lost


def test_quote_outcomes_skips_unresolvable_events() -> None:
    rows = [
        _row("oddsapi", "dk", "Kansas City Royals", "0.55", "2026-06-09T23:30:00+00:00"),
        _row("oddsapi", "dk", "Texas Rangers", "0.50", "2026-06-09T23:30:00+00:00"),
    ]
    assert quote_outcomes(rows, lambda ek: None, load_aliases()) == []  # no final score


def _insert_quote(con, venue, book, outcome, cost, fetched_at):
    con.execute(
        """INSERT INTO quotes(fetched_at, event_key, sport, venue, book, market_type, outcome,
               raw_price, cost, fee_cost, live)
           VALUES (?, ?, 'baseball_mlb', ?, ?, 'WINNER', ?, ?, ?, '0', 0)""",
        (fetched_at, EVENT, venue, book, outcome, cost, cost),
    )


def test_population_binaries_end_to_end(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        _insert_quote(con, "oddsapi", "dk", "Kansas City Royals", "0.55", "2026-06-09T23:30:00+00:00")
        _insert_quote(con, "oddsapi", "dk", "Texas Rangers", "0.50", "2026-06-09T23:30:00+00:00")
        # an in-play quote (live=1) must be ignored by the pregame population.
        con.execute(
            """INSERT INTO quotes(fetched_at, event_key, sport, venue, book, market_type, outcome,
                   raw_price, cost, fee_cost, live)
               VALUES ('2026-06-09T23:55:00+00:00', ?, 'baseball_mlb', 'oddsapi', 'dk',
                       'WINNER', 'Kansas City Royals', '0.90', '0.90', '0', 1)""",
            (EVENT,),
        )
    pairs = population_binaries(store, fixture_dir=FIX)
    assert len(pairs) == 2
    probs = sorted(p for p, _ in pairs)
    assert abs(probs[1] - 0.55 / 1.05) < 1e-6   # closing pregame quote, de-vigged

    # population sample flows through calibration unchanged.
    stats = calibration(store, [0.4, 0.5, 0.6], min_samples=1, flb_min_edge=0.02, data=pairs)
    assert sum(s.n for s in stats) == 2
