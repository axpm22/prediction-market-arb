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
    # tokens with no book -> no quote (unknown depth is None, never infinite)
    assert all(x.venue_market_id.split(":")[1] in books for x in quotes)


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
