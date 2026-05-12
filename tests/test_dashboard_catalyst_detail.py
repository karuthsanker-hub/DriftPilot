from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from driftpilot.catalyst.db import init_catalyst_schema, insert_event, update_enrichment
from driftpilot.catalyst.event import CatalystEvent
from driftpilot.dashboard.view_models import _catalyst_detail, _news_ticker


def _event(symbol: str = "REGN") -> CatalystEvent:
    return CatalystEvent(
        symbol=symbol,
        category="earnings",
        subcategory="report",
        pillar="micro",
        ts=datetime.now(timezone.utc),
        headline="REGN Q1 Adj. EPS $9.47 Beats $8.89 Estimate",
        source="alpaca",
        horizon_minutes=240,
        headline_hash=f"h-{symbol}",
        sentiment="positive",
        priority_modifier=0.15,
    )


def test_catalyst_detail_returns_context_qwen_and_flags(tmp_path: Path) -> None:
    db = str(tmp_path / "catalyst.sqlite3")
    init_catalyst_schema(db)
    insert_event(db, _event())
    update_enrichment(
        db,
        "h-REGN",
        "REGN",
        sentiment="positive",
        priority_modifier=0.15,
        horizon_minutes=240,
        confidence=0.42,
        context_json=(
            '{"market_cap_m":100000,"eps_beat_pct":0.9,'
            '"revenue_beat_pct":0.5,"last_4_surprises":[2.1,1.8],'
            '"headline_cluster_count":3}'
        ),
        qwen_response_json='{"sentiment":"positive","confidence":0.42}',
    )

    detail = _catalyst_detail(1, db_path=db)

    assert detail["found"] is True
    assert detail["event"]["symbol"] == "REGN"
    assert detail["context"]["eps_beat_pct"] == 0.9
    assert detail["qwen_response"]["confidence"] == 0.42
    labels = {flag["label"] for flag in detail["flags"]}
    assert "marginal beat" in labels
    assert "noise-level revenue" in labels
    assert "mega-cap small beat" in labels
    assert "stale / repeated" in labels
    assert "low confidence" in labels
    assert "possible anchor bias" in labels


def test_catalyst_detail_gracefully_handles_v1_event_without_context(tmp_path: Path) -> None:
    db = str(tmp_path / "catalyst.sqlite3")
    init_catalyst_schema(db)
    insert_event(db, _event("AAPL"))

    detail = _catalyst_detail(1, db_path=db)

    assert detail["found"] is True
    assert detail["context"] is None
    assert detail["message"] == "enriched without context"


def test_news_ticker_includes_v2_fields(tmp_path: Path) -> None:
    db = str(tmp_path / "catalyst.sqlite3")
    init_catalyst_schema(db)
    insert_event(db, _event())
    update_enrichment(
        db,
        "h-REGN",
        "REGN",
        sentiment="positive",
        priority_modifier=0.12,
        horizon_minutes=240,
        confidence=0.82,
        context_json='{"eps_beat_pct":6.5}',
        qwen_response_json='{"confidence":0.82}',
    )

    events = _news_ticker(db_path=db, limit=10, lookback_minutes=10_000_000)

    assert events[0]["id"] == 1
    assert events[0]["confidence"] == 0.82
    assert events[0]["has_context"] is True


def test_news_ticker_backward_compatible_with_v1_schema(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE catalyst_events ("
        "id INTEGER PRIMARY KEY, event_ts TEXT, symbol TEXT, category TEXT, "
        "subcategory TEXT, sentiment TEXT, headline TEXT, source TEXT, "
        "priority_modifier REAL)"
    )
    conn.execute(
        "INSERT INTO catalyst_events VALUES (1, ?, 'REGN', 'earnings', 'report', "
        "'positive', 'headline', 'alpaca', 0.1)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()

    events = _news_ticker(db_path=str(db), limit=10, lookback_minutes=10_000_000)

    assert events[0]["confidence"] is None
    assert events[0]["has_context"] is False
