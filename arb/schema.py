"""SQLite DDL (SPEC §9). Money columns are TEXT holding Decimal strings, never REAL.
Every table is INSERT-only or INSERT+terminal-row; nothing ever UPDATEs ledger rows."""

SCHEMA_VERSION = 3

# v2 additions (migrations only ever ADD):
MIGRATION_2 = [
    # live lane re-fetches shortlisted markets by their venue ids (L3)
    "ALTER TABLE quotes ADD COLUMN venue_market_id TEXT",
    # lead-lag study runs are append-only records (B5, SC5 discipline)
    """CREATE TABLE IF NOT EXISTS leadlag_runs (
        id INTEGER PRIMARY KEY, run_at TEXT, params_json TEXT, results_json TEXT)""",
]

# v3: MLB gamePk as an ENTRY-TIME fact on flat_bets — the doubleheader
# disambiguator (H3, §8.2). Nullable; rule 9 untouched (entry-time only).
MIGRATION_3 = [
    "ALTER TABLE flat_bets ADD COLUMN gamepk INTEGER",
]

TABLES = [
    """CREATE TABLE IF NOT EXISTS quotes (
        id INTEGER PRIMARY KEY, poll_id INTEGER, fetched_at TEXT, event_key TEXT,
        category TEXT, sport TEXT, venue TEXT, book TEXT, market_type TEXT, line TEXT,
        outcome TEXT, side TEXT, raw_price TEXT, cost TEXT, fee_cost TEXT,
        depth_usd TEXT, ladder_json TEXT, live INTEGER,
        bid TEXT, volume_24h TEXT, start_utc TEXT, start_grade TEXT)""",
    """CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY, started_at TEXT, kind TEXT,
        n_quotes INTEGER, n_opps INTEGER)""",
    """CREATE TABLE IF NOT EXISTS poll_venues (
        poll_id INTEGER, venue TEXT, ok INTEGER, n_quotes INTEGER, latency_ms REAL)""",
    """CREATE TABLE IF NOT EXISTS opportunities (
        id INTEGER PRIMARY KEY, poll_id INTEGER, fingerprint TEXT, position_key TEXT,
        kind TEXT, category TEXT, event_key TEXT, edge TEXT, status TEXT,
        legs_json TEXT, notes TEXT)""",
    # ENTRY-TIME FACTS ONLY, APPEND-ONLY (rule 9)
    """CREATE TABLE IF NOT EXISTS flat_bets (
        id INTEGER PRIMARY KEY, created_at TEXT, poll_id INTEGER, poll_kind TEXT,
        fingerprint TEXT, position_key TEXT, kind TEXT, category TEXT, sport TEXT,
        venue_pair TEXT, event_key TEXT, event_start_utc TEXT, start_grade TEXT,
        legs_json TEXT, stake_usd TEXT, edge_points TEXT, trusted INTEGER, lane TEXT,
        quality_flag TEXT, consensus_age_secs REAL, leg_skew_secs REAL, status TEXT,
        pnl_optimistic TEXT, reappearance_of INTEGER, notes TEXT, gamepk INTEGER)""",
    # append-only lifecycle log; kind: confirmed | lifetime_closed | settled | void
    # | reconciled | leg_failure; at most one settled row per bet (terminal)
    """CREATE TABLE IF NOT EXISTS bet_events (
        id INTEGER PRIMARY KEY, bet_id INTEGER, created_at TEXT, kind TEXT,
        payload_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS annotations (
        id INTEGER PRIMARY KEY, created_at TEXT, target_kind TEXT, target_id TEXT,
        kind TEXT, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS consensus (
        event_key TEXT, market_type TEXT, line TEXT, outcome TEXT, p_hat TEXT,
        method TEXT, n_books INTEGER, dispersion TEXT, computed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS consensus_books (
        event_key TEXT, market_type TEXT, line TEXT, outcome TEXT, book TEXT,
        raw_odds TEXT, p_devig TEXT, method TEXT, computed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS markouts (
        id INTEGER PRIMARY KEY, bet_id INTEGER, leg_index INTEGER, horizon_secs INTEGER,
        actual_offset_secs REAL, mark_cost TEXT, marked_at TEXT, in_play INTEGER)""",
    """CREATE TABLE IF NOT EXISTS heartbeats (
        process TEXT, beat_at TEXT, head_sha TEXT, pid INTEGER)""",
    """CREATE TABLE IF NOT EXISTS verdicts (
        id INTEGER PRIMARY KEY, created_at TEXT, pair_key TEXT, verdict TEXT,
        confidence TEXT, rules_sha TEXT, model TEXT, cost_usd TEXT)""",
    """CREATE TABLE IF NOT EXISTS left_on_table (
        id INTEGER PRIMARY KEY, poll_id INTEGER, fingerprint TEXT, kind TEXT,
        event_key TEXT, venue_pair TEXT, legs_json TEXT, reason TEXT, edge_points TEXT)""",
    """CREATE TABLE IF NOT EXISTS blacklist (
        fingerprint TEXT, event_key TEXT, reason TEXT, created_at TEXT)""",
]

# Minimal indexes — more slows the 600k-row/day insert path (§9)
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_quotes_event_fetched ON quotes(event_key, fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_quotes_poll ON quotes(poll_id)",
    "CREATE INDEX IF NOT EXISTS idx_flat_bets_fingerprint ON flat_bets(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_flat_bets_position ON flat_bets(position_key)",
    "CREATE INDEX IF NOT EXISTS idx_bet_events_bet ON bet_events(bet_id)",
    "CREATE INDEX IF NOT EXISTS idx_markouts_bet ON markouts(bet_id)",
]
