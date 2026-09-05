"""Schema migrations — only ever ADDs (SPEC §9). Version 1 = full v3.5 schema."""
from arb import schema


def apply(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER, applied_at TEXT)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    if current < 1:
        for ddl in schema.TABLES + schema.INDEXES:
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version VALUES (1, datetime('now'))")
    if current < 2:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(quotes)")}
        for ddl in schema.MIGRATION_2:
            if "ADD COLUMN venue_market_id" in ddl and "venue_market_id" in cols:
                continue  # fresh v1 DBs may already carry it via TABLES
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version VALUES (2, datetime('now'))")
    if current < 3:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(flat_bets)")}
        for ddl in schema.MIGRATION_3:
            if "ADD COLUMN gamepk" in ddl and "gamepk" in cols:
                continue  # fresh DBs already carry it via TABLES
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version VALUES (3, datetime('now'))")
    if current < 4:
        for ddl in schema.MIGRATION_4:
            conn.execute(ddl)  # CREATE TABLE IF NOT EXISTS — idempotent
        conn.execute("INSERT INTO schema_version VALUES (4, datetime('now'))")
    if current < 5:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(flat_bets)")}
        for ddl in schema.MIGRATION_5:
            if "ADD COLUMN payout" in ddl and "payout" in cols:
                continue  # fresh DBs already carry it via TABLES
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version VALUES (5, datetime('now'))")
    if current < 6:
        for ddl in schema.MIGRATION_6:
            conn.execute(ddl)  # CREATE INDEX IF NOT EXISTS — idempotent
        conn.execute("INSERT INTO schema_version VALUES (6, datetime('now'))")
    if current < 7:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(flat_bets)")}
        for ddl in schema.MIGRATION_7:
            if "ADD COLUMN variant" in ddl and "variant" in cols:
                continue  # fresh DBs already carry it via TABLES
            if "ADD COLUMN signal_id" in ddl and "signal_id" in cols:
                continue  # fresh DBs already carry it via TABLES
            conn.execute(ddl)
        conn.execute("INSERT INTO schema_version VALUES (7, datetime('now'))")
    if current < 8:
        for ddl in schema.MIGRATION_8:
            conn.execute(ddl)  # CREATE TABLE IF NOT EXISTS — idempotent
        conn.execute("INSERT INTO schema_version VALUES (8, datetime('now'))")
    if current < 9:
        for ddl in schema.MIGRATION_9:
            conn.execute(ddl)  # CREATE TABLE/INDEX IF NOT EXISTS — idempotent
        conn.execute("INSERT INTO schema_version VALUES (9, datetime('now'))")
    conn.commit()
