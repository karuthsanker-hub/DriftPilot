"""Tests for YAML prompt loader."""

from __future__ import annotations

import pytest

from driftpilot.agents.prompt_loader import PromptConfig, PromptLoader


@pytest.fixture
def prompts_dir(tmp_path):
    """Create a temp directory with test prompt YAML files."""
    d = tmp_path / "prompts"
    d.mkdir()

    (d / "test_approve.yaml").write_text(
        """\
version: 2
model: qwen
timeout_ms: 500
max_tokens: 256
fallback_action: approve
temperature: 0.0

system: |
  You are a test agent.
  Make decisions in JSON.

user_template: |
  Symbol: {symbol}
  Price: ${price}
  Decision:

response_schema:
  type: object
  required: [decision]
  properties:
    decision:
      type: string
      enum: [approve, deny]
"""
    )

    (d / "test_hold.yaml").write_text(
        """\
version: 1
model: claude
timeout_ms: 3000
max_tokens: 512
fallback_action: hold
temperature: 0.1

system: You are a slot agent.

user_template: |
  Position: {symbol} at {entry_price}
"""
    )

    # Schema/meta file (should be skipped)
    (d / "_schema.yaml").write_text("type: meta\n")

    return d


class TestPromptLoader:
    def test_loads_all_prompts(self, prompts_dir):
        loader = PromptLoader(prompts_dir)
        assert "test_approve" in loader.available_prompts
        assert "test_hold" in loader.available_prompts
        assert len(loader.available_prompts) == 2

    def test_skips_underscore_files(self, prompts_dir):
        loader = PromptLoader(prompts_dir)
        assert "_schema" not in loader.available_prompts

    def test_get_returns_prompt_config(self, prompts_dir):
        loader = PromptLoader(prompts_dir)
        config = loader.get("test_approve")

        assert isinstance(config, PromptConfig)
        assert config.name == "test_approve"
        assert config.version == "2"
        assert config.model == "qwen"
        assert config.timeout_ms == 500
        assert config.max_tokens == 256
        assert config.fallback_action == "approve"
        assert config.temperature == 0.0
        assert "test agent" in config.system

    def test_get_missing_raises_key_error(self, prompts_dir):
        loader = PromptLoader(prompts_dir)
        with pytest.raises(KeyError, match="nonexistent"):
            loader.get("nonexistent")

    def test_render_user_template(self, prompts_dir):
        loader = PromptLoader(prompts_dir)
        config = loader.get("test_approve")
        rendered = config.render_user(symbol="AAPL", price="150.00")
        assert "AAPL" in rendered
        assert "$150.00" in rendered

    def test_render_missing_var_raises(self, prompts_dir):
        loader = PromptLoader(prompts_dir)
        config = loader.get("test_approve")
        with pytest.raises(KeyError):
            config.render_user(symbol="AAPL")  # missing 'price'

    def test_reload_picks_up_changes(self, prompts_dir):
        loader = PromptLoader(prompts_dir)
        assert loader.get("test_approve").version == "2"

        # Modify the file on disk
        (prompts_dir / "test_approve.yaml").write_text(
            """\
version: 3
model: qwen
timeout_ms: 600
max_tokens: 128
fallback_action: deny
temperature: 0.0
system: Updated system prompt.
user_template: Updated template {symbol}
"""
        )

        count = loader.reload()
        assert count == 2
        config = loader.get("test_approve")
        assert config.version == "3"
        assert config.timeout_ms == 600
        assert config.fallback_action == "deny"

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        loader = PromptLoader(tmp_path / "no_such_dir")
        assert loader.available_prompts == []


class TestRealPromptConfigs:
    """Validate the actual config/prompts/*.yaml files can be loaded."""

    def test_load_production_prompts(self):
        loader = PromptLoader("config/prompts")
        assert "pm_entry_approval" in loader.available_prompts
        assert "pm_session_adaptation" in loader.available_prompts
        assert "scanner_override" in loader.available_prompts
        assert "slot_exit_override" in loader.available_prompts

    def test_pm_entry_approval_has_required_fields(self):
        loader = PromptLoader("config/prompts")
        config = loader.get("pm_entry_approval")
        assert config.model == "qwen"
        assert config.timeout_ms == 5000
        assert config.fallback_action == "approve"
        assert "{symbol}" in config.user_template
        assert "{daily_pnl_pct}" in config.user_template

    def test_slot_exit_override_defaults_to_follow_algo(self):
        loader = PromptLoader("config/prompts")
        config = loader.get("slot_exit_override")
        assert config.fallback_action == "follow_algo"
        assert config.model == "qwen"
