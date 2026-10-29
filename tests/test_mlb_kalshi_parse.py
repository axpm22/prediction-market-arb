from arb.mlb.kalshi_pull import parse_event_ticker, parse_market, split_teams

CODES = {"MIA", "AZ", "NYM", "LAD", "STL", "LAA", "TB", "MIL", "SD", "SF"}


def test_event_ticker():
    assert parse_event_ticker("KXMLBHR-26SEP152140MIAAZ") == ("KXMLBHR", "2026-09-15", "21:40", "MIAAZ")
    assert parse_event_ticker("KXMLBGAME-26OCT282000MILTB")[1:3] == ("2026-10-28", "20:00")
    assert parse_event_ticker("KXMLBGAME-25JUL20DETTEX") == ("KXMLBGAME", "2025-07-20", None, "DETTEX")


def test_split_teams():
    assert split_teams("MIAAZ", CODES) == [("MIA", "AZ")]
    assert split_teams("NYMLAD", CODES) == [("NYM", "LAD")]


def _p(t, series, title, ysub, teams=()):
    return parse_market(dict(ticker=t, series=series, title=title, yes_sub_title=ysub, teams=teams))


def test_game_total_hr_ks():
    assert _p("KXMLBGAME-26SEP152140MIAAZ-AZ", "KXMLBGAME", "Arizona wins", "Arizona")["team"] == "AZ"
    assert _p("KXMLBTOTAL-26JUL202210STLLAA-9", "KXMLBTOTAL", "Over 8.5 runs scored", "Over 8.5 runs scored")["line"] == 8.5
    r = _p("KXMLBHR-26SEP152140MIAAZ-AZKMARTE4-1", "KXMLBHR", "Ketel Marte: 1+ home runs?", "Ketel Marte: 1+", ("MIA", "AZ"))
    assert (r["player"], r["k"], r["team_code"]) == ("Ketel Marte", 1, "AZ")
    r = _p("KXMLBKS-26JUL202210STLLAA-STLKLEAHY62-5", "KXMLBKS", "Kyle Leahy: 5+ strikeouts?", "Kyle Leahy: 5+", ("STL", "LAA"))
    assert (r["player"], r["k"], r["team_code"]) == ("Kyle Leahy", 5, "STL")


def test_spread_teamtotal():
    r = _p("KXMLBSPREAD-26SEP151840CWSCLE-CLE2", "KXMLBSPREAD", "Cleveland wins by over 1.5 runs?", "Cleveland wins by over 1.5 runs")
    assert (r["team"], r["line"]) == ("CLE", 1.5)
    r = _p("KXMLBTEAMTOTAL-26SEP152140MIAAZ-AZ8", "KXMLBTEAMTOTAL", "Will Arizona score over 7.5 runs?", "Arizona over 7.5 runs scored")
    assert (r["team"], r["line"]) == ("AZ", 7.5)
