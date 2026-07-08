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
    # future migrations: `if current < 2: ... ALTER TABLE ... ADD COLUMN ...` — adds only
    conn.commit()
