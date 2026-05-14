from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trading_bot.dashboard.app import create_app


def test_pipeline_endpoint_adds_unrealized_pnl_from_position_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data" / "driftpilot"
    data_dir.mkdir(parents=True)
    (data_dir / "pipeline_log.json").write_text("[]")
    db_path = data_dir / "operator_state.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE slots (slot_id INTEGER, status TEXT, symbol TEXT, metadata_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE positions ("
            "symbol TEXT, quantity REAL, entry_price REAL, target_price REAL, "
            "stop_price REAL, status TEXT, opened_at TEXT, metadata_json TEXT)"
        )
        conn.execute(
            "INSERT INTO slots VALUES (?, ?, ?, ?)",
            (1, "OPEN", "AAPL", json.dumps({"entry_price": 100.0})),
        )
        conn.execute(
            "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "AAPL",
                5,
                100.0,
                102.0,
                99.0,
                "open",
                datetime(2026, 5, 14, 14, 30, tzinfo=timezone.utc).isoformat(),
                json.dumps({"current_price": 103.0, "signal_name": "earnings_report_v1"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    client = TestClient(create_app(env_path=tmp_path / ".env"))

    payload = client.get("/api/operator/pipeline").json()

    assert payload["status"] == "ok"
    assert payload["total_unrealized_pnl"] == 15.0
    position = payload["positions"][0]
    assert position["current_price"] == 103.0
    assert position["unrealized_pnl"] == 15.0
    assert position["unrealized_pct"] == pytest.approx(3.0)
    assert position["time_held_minutes"] is not None


def test_brain_status_combines_stats_skills_experiences_and_reflections(monkeypatch) -> None:
    class _Response:
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class _HttpClient:
        def get(self, path: str, params: dict | None = None) -> _Response:
            if path == "/brain/experiences/recent":
                return _Response({"experiences": [{"experience_id": "exp-1"}]})
            if path == "/brain/reflections":
                return _Response({"reflections": [{"date": "2026-05-14"}]})
            return _Response({})

    class _BrainClient:
        def __init__(self, timeout: float = 2.0) -> None:
            self.timeout = timeout
            self._client = _HttpClient()

        def get_stats(self) -> dict:
            return {
                "total_experiences": 1,
                "active_skills": 1,
                "total_reflections": 1,
            }

        def get_skills(self, status: str = "active") -> list[dict]:
            return [{"skill_id": "skill-1", "status": status}]

    monkeypatch.setattr("driftpilot.agents.brain_client.BrainClient", _BrainClient)
    client = TestClient(create_app())

    payload = client.get("/api/brain/status").json()

    assert payload["status"] == "ok"
    assert payload["stats"]["last_reflection_date"] == "2026-05-14"
    assert payload["skills"] == [{"skill_id": "skill-1", "status": "active"}]
    assert payload["experiences"] == [{"experience_id": "exp-1"}]
    assert payload["reflections"] == [{"date": "2026-05-14"}]
