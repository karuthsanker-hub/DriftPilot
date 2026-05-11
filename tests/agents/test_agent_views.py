"""Tests for Agent Dashboard Views."""

from __future__ import annotations

import json
import sqlite3

import pytest

from driftpilot.dashboard.agent_views import (
    agent_dashboard_payload,
    agent_decision_detail,
)


@pytest.fixture
def agent_db(tmp_path):
    """Create a populated agent DB for view testing."""
    db_path = tmp_path / "agent_views_test.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT NOT NULL UNIQUE,
            msg_type TEXT NOT NULL,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            correlation_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_at TEXT,
            expired_at TEXT
        );

        CREATE TABLE agent_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            symbol TEXT,
            slot_id INTEGER,
            algo_recommendation TEXT NOT NULL,
            agent_decision TEXT NOT NULL,
            is_override INTEGER NOT NULL DEFAULT 0,
            reasoning TEXT NOT NULL,
            confidence REAL,
            llm_model TEXT NOT NULL,
            llm_latency_ms INTEGER NOT NULL,
            prompt_version TEXT NOT NULL,
            inputs_json TEXT NOT NULL,
            raw_response TEXT NOT NULL,
            outcome_pnl_pct REAL,
            outcome_correct INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE agent_state (
            agent_name TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'idle',
            last_tick_at TEXT,
            consecutive_wins INTEGER NOT NULL DEFAULT 0,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            override_count_today INTEGER NOT NULL DEFAULT 0,
            total_decisions_today INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
    """)

    # Insert agent states
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO agent_state VALUES (?,?,?,?,?,?,?,?,?)",
        ("pm", "running", now, 3, 1, 2, 15, "{}", now),
    )
    conn.execute(
        "INSERT INTO agent_state VALUES (?,?,?,?,?,?,?,?,?)",
        ("scanner", "running", now, 0, 0, 0, 5, "{}", now),
    )
    conn.execute(
        "INSERT INTO agent_state VALUES (?,?,?,?,?,?,?,?,?)",
        ("slot_0", "running", now, 1, 0, 1, 8, "{}", now),
    )

    # Insert messages
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO agent_messages (msg_id, msg_type, from_agent, to_agent, status, payload_json, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("msg-1", "ENTRY_REQUEST", "scanner", "pm", "pending", "{}", today),
    )
    conn.execute(
        "INSERT INTO agent_messages (msg_id, msg_type, from_agent, to_agent, status, payload_json, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("msg-2", "ENTRY_DECISION", "pm", "scanner", "processed", "{}", today),
    )

    # Insert decisions
    conn.execute(
        "INSERT INTO agent_decisions "
        "(agent_name, decision_type, symbol, slot_id, algo_recommendation, "
        "agent_decision, is_override, reasoning, confidence, llm_model, "
        "llm_latency_ms, prompt_version, inputs_json, raw_response, "
        "outcome_pnl_pct, outcome_correct, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pm", "entry_approval", "AAPL", None, "approve", "approve", 0,
         "strong catalyst", 0.85, "qwen-8b", 120, "pm_entry_v1",
         json.dumps({"symbol": "AAPL"}), '{"decision":"approve"}',
         0.012, 1, today),
    )
    conn.execute(
        "INSERT INTO agent_decisions "
        "(agent_name, decision_type, symbol, slot_id, algo_recommendation, "
        "agent_decision, is_override, reasoning, confidence, llm_model, "
        "llm_latency_ms, prompt_version, inputs_json, raw_response, "
        "outcome_pnl_pct, outcome_correct, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("slot_0", "exit_override", "AAPL", 0, "hold", "request_early_cut", 1,
         "losing steam", 0.78, "qwen-8b", 95, "slot_exit_v1",
         json.dumps({"symbol": "AAPL", "slot_id": 0}),
         '{"action":"request_early_cut"}', -0.003, 0, today),
    )

    conn.commit()
    conn.close()
    return db_path


class TestDashboardPayload:
    def test_full_payload_structure(self, agent_db):
        payload = agent_dashboard_payload(agent_db)
        assert payload["enabled"] is True
        assert payload["error"] is None
        assert len(payload["agents"]) == 3
        assert "override_rate" in payload
        assert "recent_decisions" in payload
        assert "message_activity" in payload
        assert "daily_summary" in payload

    def test_agent_states(self, agent_db):
        payload = agent_dashboard_payload(agent_db)
        agents = {a["name"]: a for a in payload["agents"]}
        assert "pm" in agents
        assert agents["pm"]["status"] == "running"
        assert agents["pm"]["consecutive_wins"] == 3
        assert agents["pm"]["healthy"] is True

    def test_override_rate(self, agent_db):
        payload = agent_dashboard_payload(agent_db)
        orr = payload["override_rate"]
        assert orr["total"] == 2
        assert orr["overrides"] == 1
        assert orr["rate"] == pytest.approx(0.5)
        assert orr["within_limit"] is False  # 50% > 20%

    def test_recent_decisions(self, agent_db):
        payload = agent_dashboard_payload(agent_db)
        decs = payload["recent_decisions"]
        assert len(decs) == 2
        # Most recent first
        assert decs[0]["agent_name"] in ("pm", "slot_0")

    def test_message_activity(self, agent_db):
        payload = agent_dashboard_payload(agent_db)
        ma = payload["message_activity"]
        assert ma["pending"] == 1
        assert ma["today_total"] == 2
        assert "ENTRY_REQUEST" in ma["by_type"]

    def test_daily_summary(self, agent_db):
        payload = agent_dashboard_payload(agent_db)
        ds = payload["daily_summary"]
        assert ds["total_decisions"] == 2
        assert ds["overrides"] == 1
        assert ds["approvals"] == 1
        assert ds["avg_latency_ms"] > 0

    def test_missing_db_returns_disabled(self, tmp_path):
        payload = agent_dashboard_payload(tmp_path / "missing.sqlite3")
        assert payload["enabled"] is False
        assert "not found" in payload["error"]
        assert payload["agents"] == []


class TestDecisionDetail:
    def test_found(self, agent_db):
        result = agent_decision_detail(1, agent_db)
        assert result["found"] is True
        d = result["decision"]
        assert d["agent_name"] == "pm"
        assert d["symbol"] == "AAPL"
        assert d["inputs"] is not None
        assert d["inputs"]["symbol"] == "AAPL"

    def test_not_found(self, agent_db):
        result = agent_decision_detail(999, agent_db)
        assert result["found"] is False

    def test_missing_db(self, tmp_path):
        result = agent_decision_detail(1, tmp_path / "missing.sqlite3")
        assert result["found"] is False
