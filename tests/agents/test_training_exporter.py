"""Tests for Training Data Exporter."""

from __future__ import annotations

import json
import sqlite3

import pytest

from driftpilot.agents.training_exporter import (
    ExportFilters,
    TrainingExporter,
)


@pytest.fixture
def agent_db(tmp_path):
    """Create a minimal agent_decisions table with test data."""
    db_path = tmp_path / "agent_test.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
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
        )
    """)

    # Insert test decisions
    decisions = [
        ("pm", "entry_approval", "AAPL", None, "approve", "approve", 0,
         "good setup", 0.85, "qwen-8b", 120, "pm_entry_v1",
         json.dumps({"symbol": "AAPL"}), '{"decision":"approve"}',
         0.012, 1, "2026-05-10T10:00:00"),
        ("pm", "entry_approval", "MSFT", None, "approve", "deny", 1,
         "too risky", 0.45, "qwen-8b", 150, "pm_entry_v1",
         json.dumps({"symbol": "MSFT"}), '{"decision":"deny"}',
         None, None, "2026-05-10T10:05:00"),
        ("slot_0", "exit_override", "AAPL", 0, "hold", "hold", 0,
         "momentum continues", 0.72, "qwen-8b", 95, "slot_exit_v1",
         json.dumps({"symbol": "AAPL"}), '{"action":"hold"}',
         None, None, "2026-05-10T10:10:00"),
        ("slot_1", "exit_override", "NVDA", 1, "hold", "request_early_cut", 1,
         "losing momentum", 0.80, "claude-sonnet", 1200, "slot_exit_v1",
         json.dumps({"symbol": "NVDA"}), '{"action":"request_early_cut"}',
         -0.005, 0, "2026-05-10T10:15:00"),
        ("pm", "entry_approval", "GOOG", None, "approve", "approve", 0,
         "strong beat", 0.90, "qwen-8b", 110, "pm_entry_v1",
         json.dumps({"symbol": "GOOG"}), '{"decision":"approve"}',
         0.008, 1, "2026-05-11T09:30:00"),
    ]
    for d in decisions:
        conn.execute(
            "INSERT INTO agent_decisions "
            "(agent_name, decision_type, symbol, slot_id, algo_recommendation, "
            "agent_decision, is_override, reasoning, confidence, llm_model, "
            "llm_latency_ms, prompt_version, inputs_json, raw_response, "
            "outcome_pnl_pct, outcome_correct, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            d,
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def exporter(agent_db):
    return TrainingExporter(agent_db)


class TestExportDecisions:
    def test_export_all(self, exporter):
        examples = list(exporter.export_decisions())
        assert len(examples) == 5

    def test_export_with_agent_filter(self, exporter):
        examples = list(exporter.export_decisions(
            ExportFilters(agent_name="pm")
        ))
        assert len(examples) == 3
        assert all(e.agent_name == "pm" for e in examples)

    def test_export_overrides_only(self, exporter):
        examples = list(exporter.export_decisions(
            ExportFilters(overrides_only=True)
        ))
        assert len(examples) == 2
        assert all(e.is_override for e in examples)

    def test_export_with_outcome_only(self, exporter):
        examples = list(exporter.export_decisions(
            ExportFilters(with_outcome_only=True)
        ))
        assert len(examples) == 3
        assert all(e.outcome_pnl_pct is not None for e in examples)

    def test_export_date_range(self, exporter):
        examples = list(exporter.export_decisions(
            ExportFilters(start_date="2026-05-11", end_date="2026-05-12")
        ))
        assert len(examples) == 1
        assert examples[0].symbol == "GOOG"

    def test_export_symbol_filter(self, exporter):
        examples = list(exporter.export_decisions(
            ExportFilters(symbol="AAPL")
        ))
        assert len(examples) == 2  # entry + exit_override

    def test_export_limit(self, exporter):
        examples = list(exporter.export_decisions(
            ExportFilters(limit=2)
        ))
        assert len(examples) == 2

    def test_export_decision_type_filter(self, exporter):
        examples = list(exporter.export_decisions(
            ExportFilters(decision_type="exit_override")
        ))
        assert len(examples) == 2


class TestExportJSONL:
    def test_export_creates_file(self, exporter, tmp_path):
        output = tmp_path / "export" / "training.jsonl"
        stats = exporter.export_jsonl(output)
        assert output.exists()
        assert stats.total_decisions == 5

        lines = output.read_text().strip().split("\n")
        assert len(lines) == 5
        first = json.loads(lines[0])
        assert first["agent_name"] == "pm"
        assert first["symbol"] == "AAPL"

    def test_export_with_filters(self, exporter, tmp_path):
        output = tmp_path / "filtered.jsonl"
        stats = exporter.export_jsonl(
            output, ExportFilters(overrides_only=True)
        )
        assert stats.total_decisions == 2
        assert stats.overrides == 2


class TestGetStats:
    def test_stats_all(self, exporter):
        stats = exporter.get_stats()
        assert stats.total_decisions == 5
        assert stats.overrides == 2
        assert stats.override_rate == pytest.approx(0.4)
        assert stats.outcomes_filled == 3
        assert stats.outcomes_correct == 2
        assert stats.outcomes_incorrect == 1
        assert stats.accuracy == pytest.approx(2 / 3)
        assert "qwen-8b" in stats.models_used
        assert stats.models_used["qwen-8b"] == 4
        assert "claude-sonnet" in stats.models_used
        assert stats.avg_latency_ms > 0

    def test_stats_filtered(self, exporter):
        stats = exporter.get_stats(ExportFilters(agent_name="slot_1"))
        assert stats.total_decisions == 1
        assert stats.overrides == 1


class TestRecentDecisions:
    def test_recent_returns_ordered(self, exporter):
        recent = exporter.recent_decisions(limit=3)
        assert len(recent) == 3
        # Most recent first
        assert recent[0]["symbol"] == "GOOG"
        assert recent[1]["symbol"] == "NVDA"

    def test_recent_truncates_reasoning(self, exporter):
        recent = exporter.recent_decisions()
        for d in recent:
            assert len(d["reasoning"]) <= 200


class TestMissingDB:
    def test_missing_db_raises(self, tmp_path):
        exporter = TrainingExporter(tmp_path / "nonexistent.sqlite3")
        with pytest.raises(FileNotFoundError):
            list(exporter.export_decisions())

    def test_recent_on_missing_db(self, tmp_path):
        exporter = TrainingExporter(tmp_path / "nonexistent.sqlite3")
        # recent_decisions catches exceptions and returns []
        assert exporter.recent_decisions() == []


class TestBackfillOutcomes:
    def test_backfill_missing_operator_db(self, exporter, tmp_path):
        updated = exporter.backfill_outcomes(tmp_path / "missing.sqlite3")
        assert updated == 0
