"""Tests for agent factory — builds orchestrator from settings."""

from __future__ import annotations

from driftpilot.agents.factory import build_orchestrator
from driftpilot.settings import DriftPilotSettings


class TestBuildOrchestrator:
    def test_disabled_by_default(self):
        settings = DriftPilotSettings()
        orch = build_orchestrator(settings)
        assert orch.enabled is False
        assert orch.running is False

    def test_enabled_creates_configured_orchestrator(self, tmp_path):
        settings = DriftPilotSettings(
            agent_enabled=True,
            agent_num_slots=5,
            agent_qwen_url="http://localhost:9999/v1",
            agent_qwen_timeout_ms=100,
            agent_db_path=str(tmp_path / "test.sqlite3"),
        )
        orch = build_orchestrator(settings)
        assert orch.enabled is True
        assert orch._config.num_slots == 5
        assert orch._config.qwen_url == "http://localhost:9999/v1"

    def test_start_stop_lifecycle(self, tmp_path):
        settings = DriftPilotSettings(
            agent_enabled=True,
            agent_num_slots=2,
            agent_db_path=str(tmp_path / "lifecycle.sqlite3"),
        )
        orch = build_orchestrator(settings)
        orch.start()
        assert orch.running is True
        assert len(orch._slots) == 2
        orch.stop()
        assert orch.running is False

    def test_disabled_start_is_noop(self):
        settings = DriftPilotSettings(agent_enabled=False)
        orch = build_orchestrator(settings)
        orch.start()
        assert orch.running is False

    def test_settings_mapping(self, tmp_path):
        settings = DriftPilotSettings(
            agent_enabled=True,
            agent_claude_api_key="test-key",
            agent_claude_model="claude-sonnet-4-20250514",
            agent_max_override_rate=0.15,
            agent_prompts_dir="config/prompts",
            agent_message_ttl_seconds=600,
            agent_db_path=str(tmp_path / "map.sqlite3"),
        )
        orch = build_orchestrator(settings)
        cfg = orch._config
        assert cfg.claude_api_key == "test-key"
        assert cfg.claude_model == "claude-sonnet-4-20250514"
        assert cfg.max_override_rate == 0.15
        assert cfg.prompts_dir == "config/prompts"
        assert cfg.message_ttl_seconds == 600
