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
    confidence      REAL DEFAULT NULL,
    context_json    TEXT DEFAULT NULL,
    qwen_response_json TEXT DEFAULT NULL,
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
        _ensure_optional_columns(conn)
        conn.commit()
    finally:
        conn.close()


def _ensure_optional_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(catalyst_events)").fetchall()}
    for name, ddl in {
        "confidence": "ALTER TABLE catalyst_events ADD COLUMN confidence REAL DEFAULT NULL",
        "context_json": "ALTER TABLE catalyst_events ADD COLUMN context_json TEXT DEFAULT NULL",
        "qwen_response_json": "ALTER TABLE catalyst_events ADD COLUMN qwen_response_json TEXT DEFAULT NULL",
    }.items():
        if name not in existing:
            conn.execute(ddl)


def update_enrichment(
    db_path: str,
    headline_hash: str,
    symbol: str,
    *,
    sentiment: str | None,
    priority_modifier: float,
    horizon_minutes: int,
    confidence: float | None = None,
    context_json: str | None = None,
    qwen_response_json: str | None = None,
) -> int:
    """Patch an already-inserted event row with Qwen enrichment results.

    Called after insert_event() succeeds and the enricher returns. Without
    this, the DB row stays at sentiment=NULL forever (the bus carries the
    enriched copy but the DB doesn't), which breaks bootstrap-on-restart,
    the news ticker's sentiment tags, and the negative-catalyst gate.
    """
    conn = sqlite3.connect(db_path)
    try:
        _ensure_optional_columns(conn)
        cur = conn.execute(
            "UPDATE catalyst_events SET sentiment = ?, priority_modifier = ?, "
            "confidence = ?, context_json = ?, qwen_response_json = ?, "
            "horizon_minutes = ? WHERE headline_hash = ? AND symbol = ?",
            (
                sentiment,
                priority_modifier,
                confidence,
                context_json,
                qwen_response_json,
                horizon_minutes,
                headline_hash,
                symbol,
            ),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def insert_event(db_path: str, event: CatalystEvent) -> int:
    """Returns 1 if inserted, 0 if duplicate (UNIQUE constraint hit)."""
    conn = sqlite3.connect(db_path)
    try:
        try:
            cur = conn.execute(
                "INSERT INTO catalyst_events "
                "(event_ts, ingested_ts, symbol, category, subcategory, pillar, "
                "sentiment, priority_modifier, horizon_minutes, headline, headline_hash, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
