from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from driftpilot.catalyst.db import init_catalyst_schema, insert_event, update_enrichment
from driftpilot.catalyst.event import CatalystEvent


def _event(headline_hash: str = "h1", symbol: str = "AAPL") -> CatalystEvent:
    return CatalystEvent(
        symbol=symbol,
        category="earnings",
        subcategory="report",
        pillar="micro",
        ts=datetime(2026, 5, 3, 14, 30, tzinfo=timezone.utc),
        headline="Apple reports earnings",
        source="alpaca",
        horizon_minutes=60,
        headline_hash=headline_hash,
    )


def test_init_schema_is_idempotent(tmp_path) -> None:
    db_path = str(tmp_path / "catalyst.db")
    init_catalyst_schema(db_path)
    init_catalyst_schema(db_path)  # second call must not raise


def test_insert_event_dedup(tmp_path) -> None:
    db_path = str(tmp_path / "catalyst.db")
    init_catalyst_schema(db_path)
    event = _event()
    assert insert_event(db_path, event) == 1
    assert insert_event(db_path, event) == 0  # UNIQUE constraint hit


def test_insert_two_distinct_events_same_symbol(tmp_path) -> None:
    db_path = str(tmp_path / "catalyst.db")
    init_catalyst_schema(db_path)
    e1 = _event(headline_hash="h1")
    e2 = _event(headline_hash="h2")
    assert insert_event(db_path, e1) == 1
    assert insert_event(db_path, e2) == 1


def test_schema_migrates_and_persists_v2_enrichment_columns(tmp_path) -> None:
    db_path = str(tmp_path / "catalyst.db")
    init_catalyst_schema(db_path)
    assert insert_event(db_path, _event(headline_hash="h-v2", symbol="REGN")) == 1

    updated = update_enrichment(
        db_path,
        "h-v2",
        "REGN",
        sentiment="positive",
        priority_modifier=0.12,
        horizon_minutes=240,
        confidence=0.82,
        context_json='{"eps_beat_pct":6.5}',
        qwen_response_json='{"confidence":0.82}',
    )

    assert updated == 1
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT confidence, context_json, qwen_response_json FROM catalyst_events"
    ).fetchone()
    conn.close()
    assert row == (0.82, '{"eps_beat_pct":6.5}', '{"confidence":0.82}')
