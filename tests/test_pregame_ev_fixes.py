"""PREGAME_EV_FIX_PROMPT items 1-7 — regression tests.

The fixes themselves shipped with the prior EV# work; these tests lock each behavior the
prompt specifies and were missing or only partially covered (decay touched only the note, not
the stored numbers; staleness had no live-quote case; the profit floor, line-move time, SE
definition, calibrated-SE scaling, and spread key had no dedicated test).
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.cli import compute_line_times, detect_ev_from_consensus
from arb.config import load_config
from arb.models import Quote
from arb.pricing import consensus_dispersion, consensus_outcome_key, consensus_outcome_times
from arb.store import Store
from arb.venues.oddsapi import parse_odds_events

EVENT = "baseball_mlb:2026-06-12:kansas city royals:texas rangers"
OUTCOME = "kansas city royals"


def _quote(cost: str, *, live: bool = False, days_out: int = 1) -> Quote:
    c = Decimal(cost)
    return Quote(event_key=EVENT, venue="kalshi", book="KX", market_type="WINNER", line=None,
                 outcome_label=OUTCOME, raw_price=c, cost=c, fee_cost=Decimal("0"),
                 depth_usd=Decimal("500"), live=live, fetched_at=datetime.now(timezone.utc),
                 start_utc=datetime.now(timezone.utc) + timedelta(days=days_out),
                 home=OUTCOME, away="texas rangers")


def _fresh(p="0.56"):
    return {(EVENT, OUTCOME): (Decimal(p), datetime.now(timezone.utc))}


# ---- 1. crash guard on bad cost ----

def test_zero_cost_quote_is_skipped_not_raised() -> None:
    zero = replace(_quote("0.50"), cost=Decimal("0"), raw_price=Decimal("0"), fee_cost=Decimal("0"))
    # Must not raise out of the scan loop, and produces no opportunity for the bad quote.
    assert detect_ev_from_consensus([zero], _fresh(), max_age_hours=8, trusted_pairs=set()) == []


# ---- 2. edge-decay consistency: edge_points, ratio, AND kelly all shrink ----

def test_decay_shrinks_stored_edge_ratio_and_kelly() -> None:
    config = replace(load_config(), ev_edge_decay_per_hour=Decimal("0.02"))
    now = datetime.now(timezone.utc)
    fresh = {(EVENT, OUTCOME): (Decimal("0.56"), now)}
    aged = {(EVENT, OUTCOME): (Decimal("0.56"), now - timedelta(hours=2))}
    [f] = detect_ev_from_consensus([_quote("0.50")], fresh, 8, set(), config=config, pregame_max_age_hours=8)
    [a] = detect_ev_from_consensus([_quote("0.50")], aged, 8, set(), config=config, pregame_max_age_hours=8)
    assert a.edge_points < f.edge_points           # decayed edge in points
    assert a.edge < f.edge                         # decayed EV ratio (eff_edge/cost)
    assert a.kelly_stake < f.kelly_stake           # Kelly sized off the decayed fair value
    assert any(n.startswith("edge_decayed:") for n in a.notes)


# ---- 3. absolute expected-profit floor ----

def test_min_expected_profit_floor_default_off_and_flags_when_set() -> None:
    # Default config: knob is 0.0 -> no floor, clean flag, no exp-profit note.
    [off] = detect_ev_from_consensus([_quote("0.50")], _fresh(), 8, set(), config=load_config())
    assert off.quality_flag != "below-min-profit"
    assert not any(n.startswith("exp-profit-usd:") for n in off.notes)
    # A $100 floor a $1 stake can never clear -> quarantined below-min-profit, note attached.
    config = replace(load_config(), ev_min_expected_profit_usd=Decimal("100"))
    [on] = detect_ev_from_consensus([_quote("0.50")], _fresh(), 8, set(), config=config)
    assert on.quality_flag == "below-min-profit"
    assert any(n.startswith("exp-profit-usd:") for n in on.notes)


# ---- 4. live-quote staleness uses the tighter pregame window ----

def test_live_quote_vs_old_pregame_consensus_is_stale() -> None:
    old = datetime.now(timezone.utc) - timedelta(hours=5)     # 5h > 3h pregame window
    consensus = {(EVENT, OUTCOME): (Decimal("0.56"), old)}
    # A LIVE quote would have been "fresh" under the old 8h window; the pregame window flags it.
    [opp] = detect_ev_from_consensus([_quote("0.50", live=True)], consensus, max_age_hours=8,
                                     trusted_pairs=set(), config=load_config(), pregame_max_age_hours=3)
    assert "stale-consensus" in opp.notes


# ---- 5. consensus age = line-move time, not scan time ----

def _odds_events(t1: str, t2: str):
    def book(key, last_update):
        return {"key": key, "last_update": last_update,
                "markets": [{"key": "h2h", "last_update": last_update, "outcomes": [
                    {"name": "Kansas City Royals", "price": 2.0},
                    {"name": "Texas Rangers", "price": 2.0}]}]}
    start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    return [{"commence_time": start, "home_team": "Kansas City Royals", "away_team": "Texas Rangers",
             "bookmakers": [book("dk", t1), book("fd", t2)]}]


def test_oddsapi_captures_source_update_and_line_time_is_median() -> None:
    t1, t2 = "2026-06-20T18:00:00Z", "2026-06-20T20:00:00Z"
    quotes = parse_odds_events(_odds_events(t1, t2), "baseball_mlb")
    assert all(q.source_updated_at is not None for q in quotes)          # parsed from last_update
    times = compute_line_times(quotes, min_books=2)
    [(key, ts)] = [(k, v) for k, v in times.items() if k[1] == OUTCOME]
    # median of 18:00 and 20:00 line-move times is 19:00, NOT the scan wall-clock.
    assert ts == datetime(2026, 6, 20, 19, 0, tzinfo=timezone.utc)


def test_consensus_stamped_with_line_time_round_trips(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    scan_clock = datetime(2026, 6, 20, 22, 0, tzinfo=timezone.utc)
    line_time = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)   # 2h before the scan
    store.save_consensus({(EVENT, OUTCOME): Decimal("0.55")}, scan_clock,
                         outcome_times={(EVENT, OUTCOME): line_time})
    (_p, stamped) = store.load_consensus()[(EVENT, OUTCOME)]
    assert stamped == line_time          # stamped from the line-move time, not the scan clock


# ---- 6. dispersion = standard error, in calibrated space ----

def _odds_quote(book: str, label: str, price: str) -> Quote:
    return Quote(event_key=EVENT, venue="oddsapi", book=book, market_type="WINNER", line=None,
                 outcome_label=label, raw_price=Decimal(price), cost=Decimal("0.5"),
                 fee_cost=Decimal("0"), depth_usd=None, live=False,
                 fetched_at=datetime.now(timezone.utc),
                 start_utc=datetime.now(timezone.utc) + timedelta(days=1),
                 home=OUTCOME, away="texas rangers")


def _two_sided(books):
    quotes = []
    for b, (a_odds, b_odds) in books.items():
        quotes.append(_odds_quote(b, OUTCOME, a_odds))
        quotes.append(_odds_quote(b, "texas rangers", b_odds))
    return quotes


def test_dispersion_is_standard_error_and_shrinks_with_book_count() -> None:
    two = _two_sided({"dk": ("2.0", "2.0"), "fd": ("1.5", "3.0")})
    four = _two_sided({"dk": ("2.0", "2.0"), "fd": ("1.5", "3.0"),
                       "bm": ("2.0", "2.0"), "cs": ("1.5", "3.0")})
    se2 = consensus_dispersion(two, min_books=2)[OUTCOME]
    se4 = consensus_dispersion(four, min_books=2)[OUTCOME]
    assert se2 > 0
    # Same spread, more books -> SMALLER standard error (a max-min range would be unchanged).
    assert se4 < se2


def test_calibrator_scales_se_into_calibrated_space() -> None:
    config = replace(load_config(), ev_dispersion_gate=True, ev_edge_se_min=Decimal("0"),
                     ev_dispersion_kelly_haircut=Decimal("0"))
    consensus = {(EVENT, OUTCOME): (Decimal("0.55"), datetime.now(timezone.utc))}
    disp = {(EVENT, OUTCOME): Decimal("0.04")}
    identity = ([0.0, 1.0], [0.0, 1.0])                  # slope 1, value unchanged
    # Value-preserving slope-2 map: at p=0.55 -> 0.45 + 2*(0.55-0.50) = 0.55 (unchanged), slope 2.
    slope2 = ([0.50, 0.60], [0.45, 0.65])

    def edge_se(calib):
        [opp] = detect_ev_from_consensus([_quote("0.50")], consensus, 8, set(),
                                         e_min=Decimal("0.001"), config=config,
                                         dispersion=disp, calibrator=calib)
        return Decimal(next(n.split(":", 1)[1] for n in opp.notes if n.startswith("edge_se:")))

    r_identity = edge_se(identity)
    r_slope2 = edge_se(slope2)
    # Edge is unchanged (value preserved) but the SE doubled, so the edge/SE ratio halves.
    assert r_slope2 == (r_identity / 2).quantize(Decimal("0.000001"))


# ---- 7. spread key collision ----

def test_outcome_key_uses_line_whenever_present() -> None:
    assert consensus_outcome_key("WINNER", None, OUTCOME) == OUTCOME            # no-op today
    assert consensus_outcome_key("TOTAL", Decimal("8.5"), "OVER") == "OVER:8.5"
    # A future handicap/spread line keys on the line, so it can't collide with the moneyline.
    spread = consensus_outcome_key("SPREAD", Decimal("-1.5"), OUTCOME)
    assert spread == f"{OUTCOME}:-1.5"
    assert spread != consensus_outcome_key("WINNER", None, OUTCOME)
