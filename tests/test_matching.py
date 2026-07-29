"""Matching (SPEC §8): event identity, date±1 bridge, doubleheaders, complement
map legality, sanity gates, line-key identity."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.matching import (build_groups, complement_map, line_key, load_aliases,
                          normalize_team)
from arb.models import Quote

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")
ALIASES = load_aliases()
START = datetime(2026, 7, 8, 23, 10, tzinfo=timezone.utc)  # 19:10 ET


def q(venue, home, away, outcome, side="YES", cost="0.50", sport="baseball_mlb",
      start=START, market_type="WINNER", line=None, grade="nominal"):
    return Quote(event_key="", venue=venue, book=venue, market_type=market_type,
                 line=line, outcome_label=outcome, side=side, raw_price=Decimal(cost),
                 cost=Decimal(cost), fee_cost=Decimal("0"), depth_usd=Decimal("100"),
                 ladder_json=None, live=False, fetched_at=START, category="sports",
                 sport=sport, bid=Decimal(cost) - Decimal("0.02"), volume_24h=Decimal(5),
                 home=home, away=away, start_utc=start, start_grade=grade)


def test_normalize_team_per_sport_aliases():
    assert normalize_team("baseball_mlb", "New York M", ALIASES) == "mets"
    assert normalize_team("baseball_mlb", "New York Mets", ALIASES) == "mets"
    assert normalize_team("baseball_mlb", "Chicago WS", ALIASES) == "whitesox"
    assert normalize_team("baseball_mlb", "Toronto Blue Jays", ALIASES) == "bluejays"
    # soccer NEVER falls back to last-token (Manchester United vs Atlanta United trap)
    assert normalize_team("soccer", "Manchester United", ALIASES) is None


def test_same_game_three_venues_one_event_key():
    quotes = [
        q("kalshi", "New York M", "Kansas City", "New York M"),
        q("polymarket", "New York Mets", "Kansas City Royals", "Kansas City Royals"),
        q("sx", "New York Mets", "Kansas City Royals", "New York Mets",
          start=START + timedelta(minutes=5)),
    ]
    groups = build_groups(quotes, CFG, ALIASES)
    assert len(groups) == 1
    g = groups[0]
    assert g.event.event_key == "baseball_mlb:2026-07-08:mets:royals"
    assert all(x.event_key == g.event.event_key for x in g.quotes)
    assert g.suspect is None


def test_date_bridge_rekeys_to_canonical():
    # 23:50 NY vs 00:10 NY next day: differing NY dates, one game (§8.2)
    d1 = datetime(2026, 7, 9, 3, 50, tzinfo=timezone.utc)   # 23:50 ET Jul 8
    d2 = datetime(2026, 7, 9, 4, 10, tzinfo=timezone.utc)   # 00:10 ET Jul 9
    quotes = [q("kalshi", "New York M", "Kansas City", "New York M", start=d1),
              q("sx", "New York Mets", "Kansas City Royals", "New York Mets", start=d2)]
    groups = build_groups(quotes, CFG, ALIASES)
    assert len(groups) == 1
    # canonical = earliest venue-listed start (no verified source) -> Jul 8 NY date
    assert groups[0].event.event_key == "baseball_mlb:2026-07-08:mets:royals"


def test_doubleheader_never_cross_pairs():
    g1 = START
    g2 = START + timedelta(hours=4)  # gap > doubleheader_cluster_tol_secs (7200)
    quotes = [q("kalshi", "New York M", "Kansas City", "New York M", start=g1),
              q("sx", "New York Mets", "Kansas City Royals", "New York Mets", start=g2)]
    groups = build_groups(quotes, CFG, ALIASES)
    assert len(groups) == 2
    keys = {g.event.event_key for g in groups}
    assert len(keys) == 2  # distinct keys; game-1 and game-2 quotes never combine


def test_mlb_schedule_upgrades_start_to_verified_and_gamepk():
    sched = [{"gamePk": 823210, "gameNumber": 1, "doubleHeader": "N",
              "start_utc": START, "home": "New York Mets", "away": "Kansas City Royals",
              "state": "Scheduled", "home_score": None, "away_score": None}]
    quotes = [q("kalshi", "New York M", "Kansas City", "New York M",
                start=START + timedelta(minutes=12))]
    groups = build_groups(quotes, CFG, ALIASES, mlb_schedule=sched)
    g = groups[0]
    assert g.event.start_grade == "verified"
    assert g.event.start_utc == START
    assert g.gamepk == 823210


def test_start_drift_gate_marks_suspect():
    quotes = [q("kalshi", "New York M", "Kansas City", "New York M"),
              q("sx", "New York Mets", "Kansas City Royals", "New York Mets",
                start=START + timedelta(minutes=45))]  # > max_start_drift_mins
    groups = build_groups(quotes, CFG, ALIASES)
    assert len(groups) == 1 and groups[0].suspect == "start-drift"


def test_price_gap_gate_marks_suspect():
    quotes = [q("kalshi", "New York M", "Kansas City", "New York M", cost="0.30"),
              q("sx", "New York Mets", "Kansas City Royals", "New York Mets", cost="0.50")]
    groups = build_groups(quotes, CFG, ALIASES)
    assert groups[0].suspect == "match-suspect-price"


def test_complement_map_strict_binary_only():
    # MLB winner: NO(Mets) -> YES(Royals), keyed identically however quoted
    mlb_no = q("kalshi", "New York M", "Kansas City", "mets", side="NO")
    mapped = complement_map(mlb_no, other_outcome="royals")
    assert (mapped.outcome_label, mapped.side) == ("royals", "YES")
    # soccer: NO(TeamA) = {B or draw} -> must stay unmapped (draw-hole fake arb)
    soc = q("polymarket", "Arsenal", "Chelsea", "arsenal", side="NO", sport="soccer")
    assert complement_map(soc, other_outcome="chelsea") is soc
    # integer-line totals: NO(Over 9) = under-or-push, never UNDER 9
    tot_int = q("kalshi", "New York M", "Kansas City", "OVER", side="NO",
                market_type="TOTAL", line="9")
    assert complement_map(tot_int, other_outcome="UNDER") is tot_int
    # half-point totals: no push state -> legal
    tot_half = q("kalshi", "New York M", "Kansas City", "OVER", side="NO",
                 market_type="TOTAL", line="8.5")
    m = complement_map(tot_half, other_outcome="UNDER")
    assert (m.outcome_label, m.side) == ("UNDER", "YES")


def test_expanded_sports_normalize_across_the_venues_naming_conventions():
    """Kalshi quotes the city, Polymarket the mascot. Both must land on one
    canonical name or the pair never forms. Strings taken from live responses
    2026-07-29, not invented."""
    # WNBA
    assert normalize_team("basketball_wnba", "Indiana", ALIASES) == "fever"
    assert normalize_team("basketball_wnba", "Indiana Fever", ALIASES) == "fever"
    assert normalize_team("basketball_wnba", "PortlandFire", ALIASES) == "fire"  # PM quirk
    assert normalize_team("basketball_wnba", "Portland", ALIASES) == "fire"
    # SX suffixes women's teams with " W" (live 2026-07-29). Explicit keys, not
    # a strip rule: normalize_team requires an alias hit and never guesses (§8.1)
    assert normalize_team("basketball_wnba", "Dallas Wings W", ALIASES) == "wings"
    assert normalize_team("basketball_wnba", "Golden State Valkyries W",
                          ALIASES) == "valkyries"
    # NFL — Kalshi disambiguates co-tenants with a suffix letter
    assert normalize_team("americanfootball_nfl", "Dallas", ALIASES) == "cowboys"
    assert normalize_team("americanfootball_nfl", "Cowboys", ALIASES) == "cowboys"
    assert normalize_team("americanfootball_nfl", "Los Angeles C", ALIASES) == "chargers"
    assert normalize_team("americanfootball_nfl", "Los Angeles R", ALIASES) == "rams"
    assert normalize_team("americanfootball_nfl", "New York G", ALIASES) == "giants"
    assert normalize_team("americanfootball_nfl", "New York J", ALIASES) == "jets"
    # tennis/mma need no table — surname+initial rule handles full names
    assert normalize_team("tennis", "Ben Shelton", ALIASES) == "shelton b"
    assert normalize_team("mma", "Mark Vologdin", ALIASES) == "vologdin m"
    # sports with no table yet stay unmatched rather than guessing
    assert normalize_team("icehockey_nhl", "Boston Bruins", ALIASES) is None


def test_soccer_aliases_cover_mls_only_and_stay_silent_elsewhere():
    """Kalshi carries 192 distinct soccer clubs, Polymarket 483, and exactly 2
    match as-written — so every pair needs an explicit entry. MLS is mapped
    (30 clubs, both venues verified live 2026-07-29); the other competitions
    deliberately resolve to None rather than guess."""
    for kalshi_name, pm_name, canon in (
            ("Portland", "Portland Timbers", "portland"),
            ("New York RB", "New York Red Bulls", "redbulls"),
            ("New York City", "New York City FC", "nycfc"),
            ("Los Angeles F", "Los Angeles FC", "lafc"),
            ("Los Angeles G", "Los Angeles Galaxy", "lagalaxy"),
            ("Saint Louis", "St. Louis City SC", "stlouis"),
            ("Montreal", "CF Montréal", "montreal")):        # accent survives
        assert normalize_team("soccer", kalshi_name, ALIASES) == canon
        assert normalize_team("soccer", pm_name, ALIASES) == canon
    # the draw leg: Kalshi 'Tie', the Polymarket adapter emits 'DRAW'
    assert normalize_team("soccer", "Tie", ALIASES) == "draw"
    assert normalize_team("soccer", "DRAW", ALIASES) == "draw"
    # unmapped competitions stay unmatched — never a last-token guess
    assert normalize_team("soccer", "Manchester United", ALIASES) is None
    assert normalize_team("soccer", "Ararat-Armenia", ALIASES) is None


def test_draw_capable_sports_never_complement_map():
    """A tie/draw makes NO(A) = {B ∪ tie}. Mapping it books the draw hole as
    free money — the same trap soccer is already excluded for."""
    for sport, a, b in (("americanfootball_nfl", "cowboys", "seahawks"),  # NFL ties
                        ("cricket", "surrey", "gloucestershire"),         # draws/ties
                        ("icehockey_nhl", "bruins", "rangers")):          # unverified
        no = q("kalshi", a, b, a, side="NO", sport=sport)
        assert complement_map(no, other_outcome=b) is no, sport
    # WNBA has no tie state -> mapping is legal
    w = q("kalshi", "Indiana", "Portland", "fever", side="NO", sport="basketball_wnba")
    assert complement_map(w, other_outcome="fire").side == "YES"


def test_line_key_never_float():
    # a line parsed from a JSON number and from a string produce identical keys
    assert line_key(8.5) == line_key("8.5") == "8.5"
    assert line_key(9) == line_key("9.0") == "9"
    assert line_key(None) is None


def test_unmatched_names_never_forced():
    quotes = [q("kalshi", "Mystery Team", "Kansas City", "Mystery Team")]
    groups = build_groups(quotes, CFG, ALIASES)
    assert groups == []  # unmatched -> logged, never forced
