"""Venue adapters, fixtures-first (SPEC §6, §16 Phase 3). Every expected value
below was extracted by hand from the recorded fixture, never from the parser."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load

FIX = Path(__file__).resolve().parent / "fixtures"
CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")


def _fx(name):
    return json.loads((FIX / name).read_text())


# ---------- Kalshi ----------

def test_kalshi_parses_winner_fixture():
    from arb.venues.kalshi import parse_markets
    quotes = parse_markets(CFG, _fx("kalshi_markets_winner.json")["markets"],
                           sport="baseball_mlb", market_type="WINNER")
    assert quotes, "no quotes parsed"
    q = [x for x in quotes if x.venue_market_id == "KXMLBGAME-26JUL111410ATHCWS-CWS"
         and x.side == "YES"][0]
    assert q.cost == Decimal("0.5700")
    assert q.bid == Decimal("0.4200")
    assert q.fee_cost == Decimal("0.07") * Decimal("0.5700") * Decimal("0.4300")
    assert q.outcome_label == "Chicago WS"
    assert q.market_type == "WINNER" and q.line is None
    assert q.volume_24h == Decimal("0.00")


def test_kalshi_start_parses_from_ticker_never_close_time():
    from arb.venues.kalshi import parse_start
    # 26JUL081910 = 2026-07-08 19:10 America/New_York = 23:10Z (verified facts)
    assert parse_start("KXMLBTOTAL-26JUL081910KCNYM-9") == \
        datetime(2026, 7, 8, 23, 10, tzinfo=timezone.utc)
    assert parse_start("KXMLBALCY-26-ABC") is None  # unparseable -> None, NEVER close_time


def test_kalshi_unparseable_start_is_absent_grade():
    from arb.venues.kalshi import parse_markets
    rows = _fx("kalshi_markets_winner.json")["markets"][:1]
    rows[0] = dict(rows[0], ticker="KXMLBGAME-NODATE-CWS", event_ticker="KXMLBGAME-NODATE")
    quotes = parse_markets(CFG, rows, sport="baseball_mlb", market_type="WINNER")
    assert all(q.start_grade == "absent" and q.start_utc is None for q in quotes)


def test_kalshi_totals_ladder_line_is_string():
    from arb.venues.kalshi import parse_markets
    quotes = parse_markets(CFG, _fx("kalshi_markets_total.json")["markets"],
                           sport="baseball_mlb", market_type="TOTAL")
    q = [x for x in quotes if x.venue_market_id == "KXMLBTOTAL-26JUL081910KCNYM-9"
         and x.side == "YES"][0]
    assert q.line == "8.5" and isinstance(q.line, str)
    assert q.outcome_label == "OVER"
    assert q.cost == Decimal("0.5700")
    no = [x for x in quotes if x.venue_market_id == q.venue_market_id and x.side == "NO"][0]
    assert no.cost == Decimal("0.4400") and no.outcome_label == "OVER"


def test_kalshi_teams_parse_across_every_series_title_format():
    """Five distinct title grammars ship across the sports series (2026-07-29).
    Every expected pair below was read off the recorded fixture by hand."""
    from arb.venues.kalshi import _teams_from_title
    cases = [
        # "<A> vs <B> Winner?" — MLB, and the soccer game series
        ("Boston vs Los Angeles D Winner?", ("Boston", "Los Angeles D")),
        ("Portland vs Seattle Winner?", ("Portland", "Seattle")),
        # "Will <winner> win the <A> vs <B> <descriptor>?" — NFL, NCAAF
        ("Will Seattle win the Dallas vs Seattle Pro Football game?",
         ("Dallas", "Seattle")),
        ("Will Notre Dame win the Michigan St. vs Notre Dame college football game?",
         ("Michigan St.", "Notre Dame")),
        # "Will <player> win the <A> vs <B>: <round> match?" — ATP/WTA
        ("Will Ben Shelton win the Humbert vs Shelton: Round Of 16 match?",
         ("Humbert", "Shelton")),
        # "... professional MMA fight?" — UFC
        ("Will Mark Vologdin win the Nikolic vs Vologdin professional MMA fight?",
         ("Nikolic", "Vologdin")),
        # "<A> vs <B> <gendered> <descriptor>: <outcome>" — WNBA, cricket
        ("Indiana vs Portland women's Pro Basketball game: Portland wins?",
         ("Indiana", "Portland")),
        ("New York vs Las Vegas women's Pro Basketball game: Over 187.5 points?",
         ("New York", "Las Vegas")),
        ("Gloucestershire vs Surrey men's cricket match: Surrey wins?",
         ("Gloucestershire", "Surrey")),
    ]
    for title, expected in cases:
        assert _teams_from_title(title) == expected, title


def test_kalshi_teams_never_guess_on_an_unknown_grammar():
    """A title the grammar does not cover yields ('', '') — start_grade/matching
    treat that as unmatched. Never a half-parsed team (§8.1: no forced match)."""
    from arb.venues.kalshi import _teams_from_title
    assert _teams_from_title("Pro Basketball Conference to Win the Championship") == ("", "")
    assert _teams_from_title("Who wins the AL East?") == ("", "")


def test_kalshi_parses_tennis_mma_wnba_fixtures():
    """Live-recorded fixtures for the series added 2026-07-29."""
    from arb.venues.kalshi import parse_markets
    tennis = parse_markets(CFG, _fx("kalshi_markets_tennis.json")["markets"],
                           sport="tennis", market_type="WINNER")
    q = [x for x in tennis if x.venue_market_id == "KXATPMATCH-26JUL30HUMSHE-SHE"
         and x.side == "YES"][0]
    assert q.outcome_label == "Ben Shelton"      # full name -> normalize_team's
    # combat sports pair on the event's two full names, not the title surnames
    assert {q.away, q.home} == {"Ugo Humbert", "Ben Shelton"}
    # These series carry a DATE-ONLY ticker (26JUL30, no HHMM) -> start-unknown.
    # occurrence_datetime is NOT a fallback: on the MLB fixtures it reads
    # start+3h (the expected END, verified_facts / bug D1), so using it here
    # would invent a start three hours late. Absent stays absent.
    assert q.start_utc is None and q.start_grade == "absent"

    mma = parse_markets(CFG, _fx("kalshi_markets_mma.json")["markets"],
                        sport="mma", market_type="WINNER")
    m = [x for x in mma if x.venue_market_id == "KXUFCFIGHT-26AUG01NIKVOL-VOL"
         and x.side == "YES"][0]
    assert m.outcome_label == "Mark Vologdin"
    assert {m.away, m.home} == {"Borislav Nikolic", "Mark Vologdin"}

    wnba = parse_markets(CFG, _fx("kalshi_markets_wnba.json")["markets"],
                         sport="basketball_wnba", market_type="WINNER")
    w = [x for x in wnba if x.venue_market_id == "KXWNBAGAME-26JUL31INDPDX-PDX"
         and x.side == "YES"][0]
    assert (w.away, w.home) == ("Indiana", "Portland")
    assert w.outcome_label == "Portland"


def test_kalshi_combat_sports_pair_on_full_names_not_title_surnames():
    """Tennis/MMA titles carry SURNAMES ('Humbert vs Shelton'), which
    normalize_team rejects — its surname+initial rule needs two tokens. The two
    full names live in the event's two yes_sub_titles. Without this, every
    tennis and MMA quote is unmatched and no cross-venue pair can ever form."""
    from arb.matching import load_aliases, normalize_team
    from arb.venues.kalshi import parse_markets
    aliases = load_aliases()
    quotes = parse_markets(CFG, _fx("kalshi_markets_tennis.json")["markets"],
                           sport="tennis", market_type="WINNER")
    q = [x for x in quotes
         if x.venue_market_id == "KXATPMATCH-26JUL30HUMSHE-SHE"][0]
    assert {q.away, q.home} == {"Ugo Humbert", "Ben Shelton"}
    # both sides now survive normalization -> a pair key can form. Which name
    # lands in away vs home is fixture order and carries no meaning: matching
    # keys the pair as frozenset((a, b)) (§8.1).
    assert {normalize_team("tennis", q.away, aliases),
            normalize_team("tennis", q.home, aliases)} == {"humbert u", "shelton b"}

    mma = parse_markets(CFG, _fx("kalshi_markets_mma.json")["markets"],
                        sport="mma", market_type="WINNER")
    m = [x for x in mma if x.venue_market_id == "KXUFCFIGHT-26AUG01NIKVOL-VOL"][0]
    assert {m.away, m.home} == {"Borislav Nikolic", "Mark Vologdin"}


def test_kalshi_team_sports_still_take_teams_from_the_title():
    """The full-name join is combat-sports only: a WNBA yes_sub_title is one
    team, and using it for both sides would collapse away==home."""
    from arb.venues.kalshi import parse_markets
    quotes = parse_markets(CFG, _fx("kalshi_markets_wnba.json")["markets"],
                           sport="basketball_wnba", market_type="WINNER")
    q = [x for x in quotes
         if x.venue_market_id == "KXWNBAGAME-26JUL31INDPDX-PDX"][0]
    assert (q.away, q.home) == ("Indiana", "Portland")


def test_kalshi_empty_book_trap():
    from arb.venues.kalshi import parse_markets
    row = dict(_fx("kalshi_markets_winner.json")["markets"][0],
               yes_ask_dollars="1.0000", yes_bid_dollars="0.0000",
               no_ask_dollars="1.0000", no_bid_dollars="0.0000",
               liquidity_dollars="0.0000")
    assert parse_markets(CFG, [row], sport="baseball_mlb", market_type="WINNER") == []


# ---------- SX (rule 10: maker-side inversion) ----------

def test_sx_taker_inversion_and_depth_from_fixture():
    from arb.venues.sx import quotes_from_orders
    market = _fx("sx_markets.json")["data"]["markets"][0]
    orders = _fx("sx_orders.json")["data"]
    quotes = quotes_from_orders(CFG, market, orders, sport="baseball_mlb")
    one = [q for q in quotes if q.outcome_label == "San Francisco Giants"][0]
    two = [q for q in quotes if q.outcome_label == "Toronto Blue Jays"][0]
    # hand-derived from fixture: best maker odds vs outcome one = 0.54250 -> cost 0.4575
    assert one.cost == Decimal("0.457500")
    assert two.cost == Decimal("0.555000")
    # taker depth = maker_stake * c/(1-c); stakes 1062.53 / 799.26 USDC
    assert one.depth_usd.quantize(Decimal("0.01")) == Decimal("896.05")
    assert two.depth_usd.quantize(Decimal("0.01")) == Decimal("996.83")
    assert one.fee_cost == Decimal("0")
    assert one.start_utc == datetime.fromtimestamp(1783539900, tz=timezone.utc)


def test_sx_no_orders_no_quote():
    from arb.venues.sx import quotes_from_orders
    market = _fx("sx_markets.json")["data"]["markets"][0]
    assert quotes_from_orders(CFG, market, [], sport="baseball_mlb") == []


# ---------- Polymarket (CLOB-only prices) ----------

def test_polymarket_prices_from_clob_never_gamma():
    from arb.venues.polymarket import quotes_from_event
    events = _fx("pm_gamma_mlb_games.json")
    book = _fx("pm_clob_book.json")
    books = {book["asset_id"]: book}
    ev = [e for e in events if any(m.get("sportsMarketType") == "moneyline"
                                   for m in e["markets"])][0]
    quotes = quotes_from_event(CFG, ev, books, sport="baseball_mlb")
    q = [x for x in quotes if x.raw_price][0]
    assert q.cost == Decimal("0.42")          # best CLOB ask
    assert q.bid == Decimal("0.41")
    assert q.cost != Decimal("0.415")         # gamma outcomePrices mid must NOT be used
    assert q.outcome_label == "Colorado Rockies"
    assert q.fee_cost == Decimal("0.03") * Decimal("0.42") * Decimal("0.58")
    assert q.ladder_json                       # full book captured (bug B4)
    assert q.volume_24h == Decimal("133.02")
    assert {q.away, q.home} == {"Colorado Rockies", "San Francisco Giants"}
    # H10: lock the outcomes[0]=away, outcomes[1]=home order the adapter assumes
    # (poly.py). Grouping is frozenset-safe so live drift is cosmetic, but pin the
    # order here so a Polymarket outcome-order flip is caught, not silently trusted.
    m = [mk for mk in ev["markets"] if mk.get("sportsMarketType") == "moneyline"][0]
    outcomes = json.loads(m["outcomes"])
    assert outcomes == ["Colorado Rockies", "San Francisco Giants"]
    assert q.away == outcomes[0] and q.home == outcomes[1]
    # tokens with no book -> no quote (unknown depth is None, never infinite)
    assert all(x.venue_market_id.split(":")[1] in books for x in quotes)


def test_polymarket_soccer_decomposes_three_way_propositions():
    """Polymarket prices a 3-way soccer game as THREE Yes/No propositions
    ('Will <team> win on <date>?', '<A> vs <B> end in a draw?'), not as one
    3-outcome market. The old code read outcomes[0]/[1] as the teams, so every
    soccer quote carried away='Yes', home='No' and could never match Kalshi."""
    from arb.venues.polymarket import quotes_from_event
    ev = [e for e in _fx("pm_gamma_soccer.json")
          if e["title"].startswith("New England")][0]
    books = {b["asset_id"]: b for b in _fx("pm_clob_books_soccer.json") if b}
    quotes = quotes_from_event(CFG, ev, books, sport="soccer")
    assert quotes, "no quotes parsed"
    # teams come from the EVENT title, never from the Yes/No outcome list
    assert all({q.away, q.home} == {"New England Revolution", "Houston Dynamo"}
               for q in quotes)
    assert {q.outcome_label for q in quotes} == {
        "New England Revolution", "Houston Dynamo", "DRAW"}
    # each proposition yields both sides; the No token is a NO, not a second YES
    for label in ("New England Revolution", "DRAW"):
        sides = {q.side for q in quotes if q.outcome_label == label}
        assert sides == {"YES", "NO"}, label
    assert all(q.side == q.venue_side for q in quotes)
    # the stale date inside the question text is NEVER a start source
    assert all(q.start_utc == datetime(2026, 8, 8, 20, 30, tzinfo=timezone.utc)
               for q in quotes)


def test_polymarket_soccer_no_side_never_complement_maps():
    """NO(Team A) in a 3-way = {B ∪ draw}. Mapping it to YES(B) books the draw
    hole as free money — the §7.1 trap soccer is excluded for."""
    from arb.matching import complement_map
    from arb.venues.polymarket import quotes_from_event
    ev = [e for e in _fx("pm_gamma_soccer.json")
          if e["title"].startswith("New England")][0]
    books = {b["asset_id"]: b for b in _fx("pm_clob_books_soccer.json") if b}
    no = [q for q in quotes_from_event(CFG, ev, books, sport="soccer")
          if q.side == "NO" and q.outcome_label == "New England Revolution"][0]
    assert complement_map(no, other_outcome="Houston Dynamo") is no


# ---------- Odds API + MLB Stats ----------

def test_oddsapi_parses_real_fixture():
    # fixture = REAL response recorded 2026-07-08 (x-requests-last: 2 verified)
    from arb.venues.oddsapi import parse_events
    rows = parse_events(_fx("oddsapi_h2h.json")["events"], sport_key="baseball_mlb")
    ev0 = [r for r in rows if r["home"] == "San Francisco Giants"]
    h2h = [r for r in ev0 if r["market"] == "h2h"]
    assert len(h2h) == 10  # 5 books x 2 outcomes (betrivers listed totals only)
    dk = [r for r in h2h if r["book"] == "draftkings"
          and r["outcome"] == "San Francisco Giants"][0]
    assert dk["odds"] == Decimal("9.7")
    tot = [r for r in ev0 if r["market"] == "totals"]
    # line keys are canonical Decimal strings: 11.0 -> "11", never "11.0" (§8.3)
    assert {r["line"] for r in tot} == {"11.5", "11"}


def test_mlbstats_schedule_fixture():
    from arb.venues.mlbstats import parse_schedule
    games = parse_schedule(_fx("mlb_schedule.json"))
    g = games[0]
    assert g["gamePk"] == 823202
    assert g["away"] == "Toronto Blue Jays" and g["home"] == "San Francisco Giants"
    assert g["start_utc"] == datetime(2026, 7, 8, 19, 45, tzinfo=timezone.utc)
    assert g["gameNumber"] == 1


def test_fetch_negrisk_events_dedupes_across_overlapping_tags(monkeypatch):
    # H10: overlapping tags return the same event; fetch_negrisk_events must
    # yield it once, not once per tag (else duplicate NEGRISK candidates).
    import arb.venues.polymarket as pm
    ev = {"id": "E1", "negRisk": True}
    monkeypatch.setattr(pm, "fetch_gamma_events", lambda cfg, tag, **k: [ev])
    out = pm.fetch_negrisk_events({"negrisk": {"scan_pages": 3}},
                                  ["politics", "us-politics"])
    assert len(out) == 1 and out[0]["id"] == "E1"


def test_sx_ladder_units_are_contracts():
    # sizing/capacity walk ladder sizes as CONTRACT counts (the polymarket
    # convention); sx stored USD taker depth in the same slot, so the shared
    # consumer misread every stored sx ladder by a factor of cost
    from arb.sizing import parse_ladder
    from arb.venues.sx import quotes_from_orders
    market = _fx("sx_markets.json")["data"]["markets"][0]
    orders = _fx("sx_orders.json")["data"]
    one = [q for q in quotes_from_orders(CFG, market, orders, "baseball_mlb")
           if q.outcome_label == "San Francisco Giants"][0]
    price, size = parse_ladder(one.ladder_json)[0]
    assert price == one.cost
    # contracts x cost = USD taker depth (depth_usd stays in USD, unchanged)
    assert (size * price).quantize(Decimal("0.01")) == \
        one.depth_usd.quantize(Decimal("0.01"))


# ---------- TLS block cache (ANJ regulator block, non-retryable SSL) ----------

def _fake_ssl_session(calls, ssl_hosts=("polymarket",)):
    import requests

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class FakeSession:
        def request(self, method, url, **kw):
            calls.append(url)
            if any(h in url for h in ssl_hosts):
                raise requests.exceptions.SSLError(
                    "certificate verify failed: hostname mismatch CN=*.anj.fr")
            return Resp()

    return FakeSession()


def test_sslerror_is_non_retryable(monkeypatch):
    import pytest

    import arb.venues.base as base
    calls, sleeps = [], []
    monkeypatch.setattr(base, "_tls_block", {})
    monkeypatch.setattr(base, "_session", lambda: _fake_ssl_session(calls))
    monkeypatch.setattr(base.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(base.VenueError):
        base.get_json("https://clob.polymarket.com/books", CFG)
    assert calls == ["https://clob.polymarket.com/books"]  # exactly ONE attempt
    assert sleeps == []                                     # no backoff ladder


def test_tls_block_short_circuits_same_host_only(monkeypatch):
    import pytest

    import arb.venues.base as base
    calls = []
    monkeypatch.setattr(base, "_tls_block", {})
    monkeypatch.setattr(base, "_session", lambda: _fake_ssl_session(calls))
    with pytest.raises(base.VenueError):
        base.get_json("https://clob.polymarket.com/books", CFG)
    n = len(calls)
    with pytest.raises(base.VenueError):
        base.post_json("https://clob.polymarket.com/books", CFG, {})
    assert len(calls) == n  # blocked host: session never touched
    # other hosts unaffected
    assert base.get_json("https://api.elections.kalshi.com/x", CFG) == {"ok": True}


def test_tls_block_is_cache_not_ban(monkeypatch):
    import pytest

    import arb.venues.base as base
    calls = []
    monkeypatch.setattr(base, "_tls_block", {})
    monkeypatch.setattr(base, "_session", lambda: _fake_ssl_session(calls))
    with pytest.raises(base.VenueError):
        base.get_json("https://clob.polymarket.com/books", CFG)
    # expire the window: the host must be probed again, no restart needed
    base._tls_block["clob.polymarket.com"] = base.time.monotonic() - 1
    with pytest.raises(base.VenueError):
        base.get_json("https://clob.polymarket.com/books", CFG)
    assert len(calls) == 2


def test_oddsapi_http_error_never_leaks_the_key(monkeypatch):
    # M3: requests embeds the full URL incl. apiKey in HTTPError; an uncaught
    # traceback bypasses the log redaction entirely
    import pytest
    import requests

    import arb.venues.oddsapi as oa
    from arb.venues.base import VenueError

    class FakeResp:
        status_code = 401
        headers = {"x-requests-remaining": "400"}

        def raise_for_status(self):
            raise requests.HTTPError(
                "401 for url: https://api.the-odds-api.com/v4/x?apiKey=SEKRET")

        def json(self):
            return []

    monkeypatch.setattr(oa.requests, "get", lambda *a, **k: FakeResp())
    with pytest.raises(VenueError) as ei:
        oa._get(CFG, "/x", {})
    assert "SEKRET" not in str(ei.value)
    assert "<redacted>" in str(ei.value)


def test_sx_league_label_decides_the_canonical_sport():
    """SX sportId is a BROAD category, not a canonical sport: id 1 is Basketball
    and on 2026-07-29 its only active league was WNBA, so every SX basketball
    quote was filed as basketball_nba and could never match the Kalshi/PM WNBA
    legs. Likewise id 3 (Baseball) carries MLB, NPB and KBO leagues, all of
    which were labelled baseball_mlb (fixture sx_leagues.json)."""
    from arb.venues.sx import league_sport
    assert league_sport("WNBA", "basketball_nba") == "basketball_wnba"
    assert league_sport("NBA", "basketball_nba") == "basketball_nba"
    assert league_sport("NPB", "baseball_mlb") == "baseball_npb"
    assert league_sport("KBO League", "baseball_mlb") == "baseball_kbo"
    assert league_sport("MLB", "baseball_mlb") == "baseball_mlb"
    # an unrecognised league keeps the sportId's default rather than being dropped
    assert league_sport("Some New Cup", "soccer") == "soccer"
    assert league_sport("Champions League_UEFA", "soccer") == "soccer"


def test_sx_fetches_each_sport_id_once():
    """basketball_nba and basketball_wnba share sportId 1. Iterating cfg['sports']
    would fetch that id twice and double every quote."""
    from arb.venues.sx import SPORT_IDS, sport_ids_to_fetch
    c = {**CFG, "sports": ["basketball_nba", "basketball_wnba", "baseball_mlb"]}
    ids = sport_ids_to_fetch(c)
    assert sorted(ids) == sorted({SPORT_IDS["basketball_nba"], SPORT_IDS["baseball_mlb"]})
    assert len(ids) == len(set(ids)), "a sportId must never be fetched twice"
