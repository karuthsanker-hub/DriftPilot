from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from driftpilot.catalyst.context_assembler import ContextAssembler
from driftpilot.catalyst.db import init_catalyst_schema, insert_event
from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.qwen_enricher import EnrichmentResult


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "enrich_catalyst_events",
    ROOT / "scripts" / "enrich_catalyst_events.py",
)
assert SPEC and SPEC.loader
enrich_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(enrich_script)


def _seed_event(db: str, *, sentiment: str | None = None) -> None:
    init_catalyst_schema(db)
    insert_event(
        db,
        CatalystEvent(
            symbol="REGN",
            category="earnings",
            subcategory="report",
            pillar="micro",
            ts=datetime(2024, 12, 19, 14, 32, tzinfo=timezone.utc),
            headline="REGN Q1 Adj. EPS $9.47 Beats $8.89 Estimate",
            source="test",
            horizon_minutes=240,
            headline_hash="h-regn",
            sentiment=sentiment,
        ),
    )
    if sentiment:
        conn = sqlite3.connect(db)
        conn.execute("UPDATE catalyst_events SET sentiment = ? WHERE id = 1", (sentiment,))
        conn.commit()
        conn.close()


def test_force_re_enrich_fetches_existing_sentiment_rows(tmp_path: Path) -> None:
    db = str(tmp_path / "events.sqlite3")
    _seed_event(db, sentiment="positive")

    normal = enrich_script._fetch_pending(db)
    forced = enrich_script._fetch_pending(db, force_re_enrich=True)

    assert normal == []
    assert len(forced) == 1


def test_update_row_v2_persists_context_and_qwen_response(tmp_path: Path) -> None:
    db = str(tmp_path / "events.sqlite3")
    _seed_event(db)

    enrich_script._update_row_v2(
        db,
        1,
        EnrichmentResult("positive", 0.12, 240, confidence=0.82),
        context_json='{"eps_beat_pct":6.5}',
        qwen_response_json='{"confidence":0.82}',
    )

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT sentiment, confidence, context_json, qwen_response_json FROM catalyst_events"
    ).fetchone()
    conn.close()
    assert row == ("positive", 0.82, '{"eps_beat_pct":6.5}', '{"confidence":0.82}')


def test_dry_run_does_not_write_to_db(tmp_path: Path) -> None:
    db = str(tmp_path / "events.sqlite3")
    _seed_event(db)
    row = enrich_script._fetch_pending(db)[0]
    assembler = ContextAssembler(db_path=db, sector_etf_5d_pct_by_etf={})

    sentiment = asyncio.run(
        enrich_script._enrich_one(
            asyncio.Semaphore(1),
            MagicMock(),
            MagicMock(),
            assembler,
            db,
            row,
            dry_run=True,
        )
    )

    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT sentiment, context_json FROM catalyst_events").fetchone()
    conn.close()
    assert sentiment == "dry_run"
    assert stored == (None, None)
