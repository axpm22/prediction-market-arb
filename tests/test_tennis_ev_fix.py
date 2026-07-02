"""golf_slate_tennis_fix Part 1 — kill the tennis fake edges. Fixtures-free: builds Quote objects
directly (no live calls) and exercises the EV correctness gates + event-level start routing.

Uses mma for the exact-alias positive branch because mma is in ev_require_exact_match AND already
has real alias entries (data/aliases.json), so the "exact match books a bet" path is testable
against production aliases without touching them."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.cli import detect_ev_from_consensus, split_pregame
from arb.config import load_config
from arb.models import Quote

NOW = datetime.now(timezone.utc)


def _quote(sport, label, cost, *, venue="polymarket", start=None, event_key=None, depth="600") -> Quote:
    return Quote(
        event_key=event_key or f"{sport}:2026-07-11:a:b",
        venue=venue, book="tok", market_type="WINNER", line=None, outcome_label=label,
        raw_price=Decimal(cost), cost=Decimal(cost), fee_cost=Decimal("0.001"),
        depth_usd=Decimal(depth), live=False, fetched_at=NOW,
        start_utc=start if start is not None else NOW + timedelta(hours=5),
        home="a", away="b",
    )


# ---- Part 1.0: event-level start, earliest reliable venue wins ------------------------------

def test_event_start_earliest_reliable_venue_routes_all_live() -> None:
    # Reproduces Bublik–Jacquet: Polymarket's nominal slot says +3h, Kalshi says already started.
    # The event is live, so BOTH quotes route to the non-actionable lane — zero pregame EV.
    ek = "tennis:2026-07-02:alexander bublik:kyrian jacquet"
    poly = _quote("tennis", "kyrian jacquet", "0.03", venue="polymarket",
                  start=NOW + timedelta(hours=3), event_key=ek)
    kalshi = _quote("tennis", "kyrian jacquet", "0.20", venue="kalshi",
                    start=NOW - timedelta(minutes=10), event_key=ek)
    pregame, in_play = split_pregame([poly, kalshi], NOW, {"polymarket": ["tennis"]})
    assert pregame == []
    assert len(in_play) == 2 and all(q.live for q in in_play)
    # Even with a fresh consensus row, nothing is bettable (detection only sees pregame).
    consensus = {(ek, "kyrian jacquet"): (Decimal("0.60"), NOW)}
    assert detect_ev_from_consensus(pregame, consensus, max_age_hours=8,
                                    trusted_pairs=set(), config=load_config()) == []


def test_unreliable_only_event_is_start_unknown() -> None:
    # An event whose ONLY start source is the unreliable venue (Polymarket tennis) has no reliable
    # start -> start unknown -> non-actionable (E5), never pregame.
    ek = "tennis:2026-07-02:x:y"
    poly = _quote("tennis", "y", "0.30", venue="polymarket", start=NOW + timedelta(hours=3), event_key=ek)
    pregame, in_play = split_pregame([poly], NOW, {"polymarket": ["tennis"]})
    assert pregame == [] and in_play[0].live is True


# ---- Part 1.1 / 1.2 / 1.3: EV correctness gates ---------------------------------------------

def test_unnormalized_outcome_creates_no_ev_bet() -> None:
    # The observed case: consensus p_hat=0.235101, Polymarket ask 0.09, outcome unnormalized (empty).
    ek = "tennis:2026-07-02:alexander bublik:kyrian jacquet"
    quote = _quote("tennis", "", "0.09", event_key=ek)
    consensus = {(ek, ""): (Decimal("0.235101"), NOW)}
    sink: list[dict] = []
    opps = detect_ev_from_consensus([quote], consensus, max_age_hours=8, trusted_pairs=set(),
                                    config=load_config(), left_on_table=sink)
    assert opps == []
    assert Counter(r["reason"] for r in sink) == Counter({"outcome-unnormalized": 1})


def test_fuzzy_surname_creates_no_ev_bet_but_exact_alias_does() -> None:
    config = load_config()
    # Fuzzy: "kyrian jacquet" is not an explicit tennis alias -> no EV bet (divergence-only path).
    ek_t = "tennis:2026-07-02:alexander bublik:kyrian jacquet"
    fuzzy = _quote("tennis", "kyrian jacquet", "0.20", event_key=ek_t)
    sink: list[dict] = []
    assert detect_ev_from_consensus([fuzzy], {(ek_t, "kyrian jacquet"): (Decimal("0.35"), NOW)},
                                    max_age_hours=8, trusted_pairs=set(), config=config,
                                    left_on_table=sink) == []
    assert sink and sink[0]["reason"] == "fuzzy-no-exact-alias"
    # Exact: "bobby green" IS an mma alias value -> the bet IS created.
    ek_m = "mma:2026-07-11:bobby green:terrance mckinney"
    exact = _quote("mma", "bobby green", "0.35", event_key=ek_m)
    opps = detect_ev_from_consensus([exact], {(ek_m, "bobby green"): (Decimal("0.45"), NOW)},
                                    max_age_hours=8, trusted_pairs=set(), config=config)
    assert len(opps) == 1 and opps[0].legs[0].outcome_label == "bobby green"


def test_edge_above_ceiling_is_implausible_no_bet() -> None:
    config = load_config()
    ek = "mma:2026-07-11:bobby green:terrance mckinney"
    # Exact alias, but a 40-point edge (cost 0.10 vs p_hat 0.50) is a data error, not opportunity.
    quote = _quote("mma", "bobby green", "0.10", event_key=ek)
    sink: list[dict] = []
    opps = detect_ev_from_consensus([quote], {(ek, "bobby green"): (Decimal("0.50"), NOW)},
                                    max_age_hours=8, trusted_pairs=set(), config=config,
                                    left_on_table=sink)
    assert opps == []
    assert sink and sink[0]["reason"] == "edge-implausible"
    assert Decimal(str(sink[0]["edge"])) > config.ev_max_edge
