from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .event import CatalystEvent

CATALYST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalyst_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ts        TIMESTAMP NOT NULL,
    ingested_ts     TIMESTAMP NOT NULL,
    symbol          TEXT NOT NULL,
    category        TEXT NOT NULL,
    subcategory     TEXT NOT NULL,
    pillar          TEXT NOT NULL,
    sentiment       TEXT,
    priority_modifier REAL DEFAULT 0,
    horizon_minutes INTEGER NOT NULL,
    headline        TEXT NOT NULL,
    headline_hash   TEXT NOT NULL,
    source          TEXT NOT NULL,
    UNIQUE(symbol, headline_hash, event_ts)
);
CREATE INDEX IF NOT EXISTS idx_catalyst_symbol_ts ON catalyst_events(symbol, event_ts);
CREATE INDEX IF NOT EXISTS idx_catalyst_active ON catalyst_events(event_ts, category, subcategory);
"""


def init_catalyst_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(CATALYST_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def insert_event(db_path: str, event: CatalystEvent) -> int:
    """Returns 1 if inserted, 0 if duplicate (UNIQUE constraint hit)."""
    conn = sqlite3.connect(db_path)
    try:
        try:
            cur = conn.execute(
                "INSERT INTO catalyst_events (event_ts, ingested_ts, symbol, category, subcategory, pillar, sentiment, priority_modifier, horizon_minutes, headline, headline_hash, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.ts.isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    event.symbol,
                    event.category,
                    event.subcategory,
                    event.pillar,
                    event.sentiment,
                    event.priority_modifier,
                    event.horizon_minutes,
                    event.headline,
                    event.headline_hash,
                    event.source,
                ),
            )
            conn.commit()
            return 1 if cur.rowcount > 0 else 0
        except sqlite3.IntegrityError:
            return 0
    finally:
        conn.close()
