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
-- golf_slate_tennis_fix Part 1: EV candidates deliberately NOT booked (edge-implausible,
-- outcome-unnormalized, fuzzy-no-exact-alias). Append-only; the daily report groups by reason
-- so nothing is a silent discard (rule 9: report recomputed from the raw log).
CREATE TABLE IF NOT EXISTS left_on_table(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER,
    reason TEXT NOT NULL,
    sport TEXT NOT NULL,
    event_key TEXT NOT NULL,
    venue TEXT,
    outcome TEXT,
    edge TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_left_on_table_created ON left_on_table(created_at);
-- Immutable bet annotations (bug:<issue>, settler-error, ...). Bets are never edited; context
-- accumulates beside them and annotated rows drop out of every clean/honest-variant report cell.
-- Created on-demand by arb.experimental.annotations too; declared here so the NOT-IN filters
-- every reader uses are safe on a fresh DB (auxiliary table only, no bet-data migration).
CREATE TABLE IF NOT EXISTS annotations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(bet_id) REFERENCES flat_bets(id)
);
-- MODEL_FOUNDATIONS Phase C: post-entry price path per bet (adverse-selection measurement).
-- Analysis data, may UPDATE (cross-cutting rule) — never feeds an append-only P&L cell directly;
-- pnl_markout is computed from it for reporting only.
CREATE TABLE IF NOT EXISTS markouts(
    bet_id INTEGER NOT NULL,
    horizon_secs INTEGER NOT NULL,
    mid TEXT,
    ask TEXT,
    bid TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(bet_id, horizon_secs)
);
-- MODEL_FOUNDATIONS Phase D: operational visibility for the multi-loop daemon. NOT a ledger —
-- one row per process, UPDATEd each beat (cross-cutting rule: operational tables may UPDATE).
CREATE TABLE IF NOT EXISTS heartbeats(
    process TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    git_head TEXT,
    dirty INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    last_beat_at TEXT NOT NULL,
    polls_done INTEGER NOT NULL DEFAULT 0,
    interval_secs INTEGER
);
CREATE TABLE IF NOT EXISTS consensus(
    event_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    p_hat TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY(event_key, outcome)
);
CREATE TABLE IF NOT EXISTS live_probe_samples(
    -- LIVE_BASEBALL_EV Phase 0 feasibility probe. Append-only raw samples (no detection, no bet):
    -- 'state' rows carry MLB Stats publish lag + game context; 'venue_quote' rows carry live
    -- top-of-book depth/suspension/age per venue; 'odds_book' rows carry the live-book count and
    -- credits_remaining. The report reduces these to the freshness/depth verdict (§5).
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,          -- OUR fetch wall clock (UTC ISO) — never a feed clock
    game_pk INTEGER NOT NULL,
    sample_type TEXT NOT NULL,          -- state | venue_quote | odds_book
    publish_lag_secs REAL,              -- state: our_fetch - feed_embedded_time
    inning INTEGER, half TEXT, outs INTEGER, game_status TEXT,
    venue TEXT, outcome_label TEXT, market_type TEXT,
    ask TEXT, bid TEXT, depth_usd TEXT, suspended INTEGER, quote_age_secs REAL,
    n_live_books INTEGER, credits_remaining INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_probe_recorded ON live_probe_samples(recorded_at);
CREATE TABLE IF NOT EXISTS live_candidates(
    -- LIVE_BASEBALL_EV Phase 1 candidate recorder. Append-only, measurement-ONLY: this lane
    -- NEVER writes flat_bets (betting is off and the promotion gate is uncleared), so its
    -- experimental rows can never leak into a paper total. Every evaluation lands here, including
    -- rejections and NO-CONSENSUS snapshots — the rejection/VANISHED mix IS the product of Phase 1.
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    game_pk INTEGER,
    fingerprint TEXT NOT NULL,       -- (event, venue, book, outcome, game-state) — dedup key
    event_key TEXT NOT NULL,
    venue TEXT, outcome_label TEXT, market_type TEXT,
    status TEXT NOT NULL,            -- FRESH_CANDIDATE | REJECTED_STALE | REJECTED_UNSTABLE | RESEARCH | NONE
    reason TEXT,
    recheck_result TEXT,             -- CONFIRMED | LIVE_VANISHED | NULL (never rechecked)
    no_consensus INTEGER NOT NULL DEFAULT 0,
    n_books INTEGER,
    raw_edge TEXT, adjusted_edge TEXT, promoted INTEGER,
    executable_cost TEXT, fair_value TEXT, depth_usd TEXT,
    effective_state_age REAL, venue_age_secs REAL,
    inning INTEGER, half TEXT, outs INTEGER, bases TEXT, score TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_candidates_recorded ON live_candidates(recorded_at);
CREATE TABLE IF NOT EXISTS nonsports_verdicts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    pair_key TEXT NOT NULL UNIQUE,
    venue_a TEXT NOT NULL,
    market_a TEXT NOT NULL,
    venue_b TEXT NOT NULL,
    market_b TEXT NOT NULL,
    equivalent INTEGER,            -- 1/0; NULL = gave up (garbage output or 3 API failures)
    confidence TEXT NOT NULL DEFAULT '0',
    differences_json TEXT,
    entity TEXT,
    metric TEXT,
    deadline TEXT,
    source_a TEXT,
    source_b TEXT,
    model TEXT,
    raw_response TEXT
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
CREATE TABLE IF NOT EXISTS wallets(
    address TEXT PRIMARY KEY,
    first_seen TEXT,
    n_trades INTEGER,
    win_rate REAL,
    pnl_usd TEXT,
    top_category TEXT,
    category_share REAL,
    qualified INTEGER,
    suspect INTEGER
);
CREATE TABLE IF NOT EXISTS wallet_trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size_usd TEXT NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0,
    result TEXT
);
CREATE TABLE IF NOT EXISTS forecasts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    made_at TEXT NOT NULL,
    vertical TEXT NOT NULL,
    category TEXT NOT NULL,
    market TEXT NOT NULL,
    p_base TEXT,
    p_you TEXT NOT NULL,
    p_market TEXT NOT NULL,
    executable_price TEXT,
    settle_by TEXT,
    settled_at TEXT,
    outcome INTEGER,
    brier_you REAL,
    brier_market REAL,
    logloss_you REAL,
    logloss_market REAL,
    clv REAL
);
-- B3 (MicroEVBitcoin.md): per-window record of both venues' BTC settlement print + realized
-- result, so cross-venue oracle agreement (CF Benchmarks BRTI vs Polymarket UMA) becomes an
-- empirical question. Append-only; the path from REVIEW -> a whitelistable trusted pair.
CREATE TABLE IF NOT EXISTS btc_oracle_prints(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    asset TEXT NOT NULL,                 -- BTC@strike@YYYYMMDDHHMM
    window_end_utc TEXT NOT NULL,
    kalshi_print TEXT,                   -- BRTI settlement price (string Decimal)
    poly_print TEXT,                     -- UMA settlement price
    kalshi_result INTEGER,               -- 1/0 binary YES outcome per Kalshi
    poly_result INTEGER                  -- 1/0 binary YES outcome per Polymarket
);
CREATE INDEX IF NOT EXISTS idx_quotes_close_lookup
ON quotes(event_key, venue, book, outcome, live, fetched_at);
-- flat_bets is scanned heavily by the dashboard, tracker, sweeps, and settlement.
-- The correlated reappearance_of subqueries (confirmed/verified/vanished/lifetime counts)
-- full-scan the table per row without this; it turns each into an index lookup.
CREATE INDEX IF NOT EXISTS idx_flatbets_reappearance ON flat_bets(reappearance_of, status);
CREATE INDEX IF NOT EXISTS idx_flatbets_kind_status ON flat_bets(kind, status);
CREATE INDEX IF NOT EXISTS idx_flatbets_status_result ON flat_bets(status, result);
CREATE INDEX IF NOT EXISTS idx_flatbets_fingerprint ON flat_bets(fingerprint, status);
-- quotes lookups by recency (focus bet-map, lead-lag) and per-event grouping.
CREATE INDEX IF NOT EXISTS idx_quotes_fetched ON quotes(fetched_at);
CREATE INDEX IF NOT EXISTS idx_quotes_event ON quotes(event_key);
"""

MIGRATION_COLUMNS = {
    "consensus": {
        # EV#3: cross-book dispersion (max-min de-vigged prob) stored beside p_hat so EV
        # detection can gate on edge/SE and size Kelly off the lower bound of fair value.
        "dispersion": "TEXT",
    },
    "quotes": {
        "poll_id": "INTEGER",
        "category": "TEXT NOT NULL DEFAULT 'sports'",
        "market_type": "TEXT NOT NULL DEFAULT 'WINNER'",
        "line": "TEXT",
        "depth_usd": "TEXT",
        "live": "INTEGER NOT NULL DEFAULT 0",
        # K12: best same-side bid so a true (bid+ask)/2 mid exists (real mid-based CLV,
        # maker-side analysis). NULL = venue didn't report one.
        "best_bid": "TEXT",
    },
    "opportunities": {
        "poll_id": "INTEGER",
        "fingerprint": "TEXT",
        "category": "TEXT NOT NULL DEFAULT 'sports'",
    },
    "flat_bets": {
        # Phase 1: NULL = clean; else labels such as 'suspect-edge', 'thin-book',
        # 'micro', or 'one-sided'.
        "quality_flag": "TEXT",
        # Canonical edge in PROBABILITY POINTS (EV: p-c, ARB: 1-S). The `edge` column is a
        # per-kind ratio (EV (p-c)/c, ARB ROI) kept for display; aggregates/thresholds that
        # must be comparable across kinds and cost levels use this instead.
        "edge_points": "TEXT",
        # Phase 3: parallel fractional-Kelly sizing (projection, never the headline).
        "kelly_f_star": "TEXT",
        "kelly_stake_usd": "TEXT",
        "pnl_kelly_optimistic": "TEXT",
        "pnl_kelly_confirmed": "TEXT",
        # S1: negative-risk basket metadata.
        "basket_n": "INTEGER",
        "basket_sum": "TEXT",
        # Validation playbook: frozen forward-test cohort labels.
        "validation_rule": "TEXT",
        "validation_tag": "TEXT",
        # K4/K5: machine-readable facts as real columns, not note prose.
        # adj_edge = skew-haircut-adjusted ARB edge (what sizing/ranking trust).
        "adj_edge": "TEXT",
        # K14: the raw model probability estimate at flag time (EV), pre push/decay haircut.
        "p_hat": "TEXT",
        # K5: flash-verify re-quote facts (the R1 confirm source).
        "requote_sum": "TEXT",
        "requote_lag_secs": "REAL",
        # Phase F (08): time facts as real columns, not note prose. consensus_age_secs = age of the
        # de-vigged consensus at bet creation (Phase A edge-decay-with-age input); leg_skew_secs =
        # max−min leg fetch time on multi-leg records (flash-verify prioritization / ghost risk).
        "consensus_age_secs": "INTEGER",
        "leg_skew_secs": "REAL",
        # Spec amendment (nonsports_ai_matcher): 1 = event equivalence decided by Haiku.
        # These bets report in their own section and never enter the headline number.
        "ai_matched": "INTEGER NOT NULL DEFAULT 0",
        # STRIKE_AWARE: a paper-settled AI-matched pair whose two venues disagreed on the
        # outcome — the fake-arb case; the row's P&L shows the damage and the pair is banned.
        "settlement_mismatch": "INTEGER NOT NULL DEFAULT 0",
    },
    # STRIKE_AWARE: strike-level facts Haiku extracted, stored for audit beside the verdict.
    "nonsports_verdicts": {
        "comparator_a": "TEXT", "comparator_b": "TEXT",
        "threshold_a": "TEXT", "threshold_b": "TEXT",
        "unit_a": "TEXT", "unit_b": "TEXT",
        "data_source_a": "TEXT", "data_source_b": "TEXT",
        "rounding_a": "TEXT", "rounding_b": "TEXT",
        # MODEL_FOUNDATIONS Phase E: the verdict is about the RULES TEXTS, not the pair. Storing
        # each side's rules hash lets a cached verdict expire when a venue edits its rules.
        "rules_hash_a": "TEXT", "rules_hash_b": "TEXT",
    },
}
