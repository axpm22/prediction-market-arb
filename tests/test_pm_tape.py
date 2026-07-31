"""PM WebSocket tape recorder. Its OWN SQLite file (rule 9 unreachable).
No sockets in tests — frames are fed as dicts."""
import sqlite3

from arb import pm_tape

BOOK = {"event_type": "book", "asset_id": "tokA", "timestamp": "1785457058983",
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.42", "size": "80"}]}
PC = {"event_type": "price_change", "timestamp": "1785457066528",
      "price_changes": [{"asset_id": "tokA", "price": "0.41", "size": "5",
                         "side": "BUY", "best_bid": "0.41", "best_ask": "0.42"}]}
# World A, verified 2026-07-30: a real trade print, which a book delta cannot
# distinguish from a cancellation.
TRADE = {"event_type": "last_trade_price", "asset_id": "tokA", "price": "0.40",
         "size": "6.44", "side": "SELL", "fee_rate_bps": "0",
         "timestamp": "1785469134091", "transaction_hash": "0x00d5"}


def test_schema_creates_in_a_standalone_file(tmp_path):
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tape_frames" in names
    # rule 9: nothing in this file can reach the ledger
    assert "flat_bets" not in names and "bet_events" not in names


def test_persist_stores_one_row_per_frame_with_receipt_time(tmp_path):
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    n = pm_tape.persist_frames(con, [BOOK, PC], [1000.5, 1001.25])
    assert n == 2
    rows = [dict(r) for r in con.execute(
        "SELECT event_type, received_at, asset_id FROM tape_frames ORDER BY id")]
    assert [r["event_type"] for r in rows] == ["book", "price_change"]
    assert rows[0]["received_at"] == 1000.5
    assert rows[0]["asset_id"] == "tokA"


def test_price_change_asset_id_is_read_from_the_change_not_the_root(tmp_path):
    """price_change frames carry no root asset_id — it lives on each entry of
    price_changes[]. Reading the root drops the id and the replay cannot key
    the frame to a book."""
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    pm_tape.persist_frames(con, [PC], [1.0])
    got = con.execute("SELECT asset_id FROM tape_frames").fetchone()[0]
    assert got == "tokA"


def test_trade_prints_are_recorded_with_their_own_event_type(tmp_path):
    """last_trade_price is the whole reason the maker lane can tell a fill
    from a cancellation. It must survive into the tape distinguishable."""
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    pm_tape.persist_frames(con, [BOOK, TRADE], [1.0, 2.0])
    row = dict(con.execute(
        "SELECT event_type, asset_id FROM tape_frames "
        "WHERE event_type='last_trade_price'").fetchone())
    assert row["asset_id"] == "tokA"


def test_raw_json_round_trips_so_replay_never_depends_on_the_parser(tmp_path):
    import json
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    pm_tape.persist_frames(con, [BOOK], [1.0])
    raw = con.execute("SELECT raw_json FROM tape_frames").fetchone()[0]
    assert json.loads(raw) == BOOK


def test_persist_is_resumable_across_connections(tmp_path):
    p = tmp_path / "t.sqlite"
    pm_tape.persist_frames(pm_tape.open_tape(p), [BOOK], [1.0])
    pm_tape.persist_frames(pm_tape.open_tape(p), [PC], [2.0])
    con = sqlite3.connect(p)
    assert con.execute("SELECT COUNT(*) FROM tape_frames").fetchone()[0] == 2
