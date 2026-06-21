SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS polls(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    kind TEXT NOT NULL,
    n_quotes INTEGER NOT NULL DEFAULT 0,
    n_opps INTEGER NOT NULL DEFAULT 0,
    credits_remaining INTEGER
);
CREATE TABLE IF NOT EXISTS scans(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    credits_remaining INTEGER,
    n_quotes INTEGER NOT NULL DEFAULT 0,
    n_arbs INTEGER NOT NULL DEFAULT 0,
    n_ev INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quotes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER,
    scan_id INTEGER,
    fetched_at TEXT NOT NULL,
    event_key TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'sports',
    sport TEXT NOT NULL,
    venue TEXT NOT NULL,
    book TEXT NOT NULL,
    market_type TEXT NOT NULL DEFAULT 'WINNER',
    line TEXT,
    outcome TEXT NOT NULL,
    raw_price TEXT NOT NULL,
    cost TEXT NOT NULL,
    fee_cost TEXT NOT NULL,
    depth_usd TEXT,
    max_size_usd TEXT,
    live INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(poll_id) REFERENCES polls(id),
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);
CREATE TABLE IF NOT EXISTS opportunities(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER,
    scan_id INTEGER,
    fingerprint TEXT,
    kind TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'sports',
    event_key TEXT NOT NULL,
    edge TEXT NOT NULL,
    status TEXT NOT NULL,
    legs_json TEXT NOT NULL,
    stakes_json TEXT NOT NULL,
    notes TEXT NOT NULL,
    FOREIGN KEY(poll_id) REFERENCES polls(id),
    FOREIGN KEY(scan_id) REFERENCES scans(id)
);
CREATE TABLE IF NOT EXISTS flat_bets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    reappearance_of INTEGER,
    opened_poll_id INTEGER NOT NULL,
    closed_poll_id INTEGER,
    category TEXT NOT NULL,
    sport TEXT NOT NULL,
    kind TEXT NOT NULL,
    trusted INTEGER NOT NULL,
    live INTEGER NOT NULL,
    edge TEXT NOT NULL,
    stake_usd TEXT NOT NULL,
    legs_json TEXT NOT NULL,
    optimistic_cost TEXT,
    confirmed_cost TEXT,
    pnl_optimistic TEXT,
    pnl_confirmed TEXT,
    status TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    settled_at TEXT,
    result TEXT,
    notes TEXT NOT NULL,
    FOREIGN KEY(opened_poll_id) REFERENCES polls(id),
    FOREIGN KEY(closed_poll_id) REFERENCES polls(id),
    FOREIGN KEY(reappearance_of) REFERENCES flat_bets(id)
);
CREATE TABLE IF NOT EXISTS suspect_matches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER,
    fingerprint TEXT,
    event_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    edge TEXT NOT NULL,
    legs_json TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    classification TEXT
);
CREATE TABLE IF NOT EXISTS consensus(
    event_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    p_hat TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY(event_key, outcome)
);
CREATE TABLE IF NOT EXISTS positions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_scan_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    legs_json TEXT NOT NULL,
    stakes_json TEXT NOT NULL,
    cost_usd TEXT NOT NULL,
    guaranteed_payout_usd TEXT,
    status TEXT NOT NULL,
    closed_at TEXT,
    pnl_usd TEXT,
    FOREIGN KEY(opened_scan_id) REFERENCES scans(id)
);
CREATE TABLE IF NOT EXISTS ledger(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    position_id INTEGER,
    amount_usd TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    FOREIGN KEY(position_id) REFERENCES positions(id)
);
"""

MIGRATION_COLUMNS = {
    "quotes": {
        "poll_id": "INTEGER",
        "category": "TEXT NOT NULL DEFAULT 'sports'",
        "market_type": "TEXT NOT NULL DEFAULT 'WINNER'",
        "line": "TEXT",
        "depth_usd": "TEXT",
        "live": "INTEGER NOT NULL DEFAULT 0",
    },
    "opportunities": {
        "poll_id": "INTEGER",
        "fingerprint": "TEXT",
        "category": "TEXT NOT NULL DEFAULT 'sports'",
    },
    "flat_bets": {
        # Phase 1: NULL = clean; else 'suspect-edge' | 'thin-book' | 'one-sided'.
        "quality_flag": "TEXT",
        # Phase 3: parallel fractional-Kelly sizing (projection, never the headline).
        "kelly_f_star": "TEXT",
        "kelly_stake_usd": "TEXT",
        "pnl_kelly_optimistic": "TEXT",
        "pnl_kelly_confirmed": "TEXT",
        # S1: negative-risk basket metadata.
        "basket_n": "INTEGER",
        "basket_sum": "TEXT",
    },
}

