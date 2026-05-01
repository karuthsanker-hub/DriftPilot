from __future__ import annotations

import json
from datetime import date

import pytest

from trading_bot.llm.base import LLMOutputValidationError
from trading_bot.llm.models import MorningInput, ProviderName
from trading_bot.llm.providers.qwen_adapter import QwenAdapter, _loads_json_object


def test_qwen_json_loader_extracts_object_after_reasoning_text() -> None:
    payload = {"date": "2026-04-28", "trading_enabled": False, "vix": 18, "watchlist": [], "regime_reason": "No setup."}
    content = f"<think>reasoning text</think>\n{json.dumps(payload)}"

    assert _loads_json_object(content) == payload


def test_qwen_json_loader_rejects_non_object() -> None:
    with pytest.raises(LLMOutputValidationError):
        _loads_json_object("[1, 2, 3]")


def test_qwen_daily_config_prompt_includes_schema_and_validates(monkeypatch) -> None:
    adapter = QwenAdapter(model="Qwen/Qwen3-8B", base_url="http://localhost:8000/v1")
    captured: dict[str, str] = {}

    def fake_chat(system: str, user: str, *, max_tokens: int, response_format=None) -> str:
        captured["system"] = system
        captured["user"] = user
        captured["response_format"] = response_format
        return json.dumps(
            {
                "date": "2026-04-28",
                "provider": "qwen",
                "model": "ignored",
                "trading_enabled": False,
                "vix": 18.0,
                "watchlist": [],
                "regime_reason": "No high-conviction setup.",
            }
        )

    monkeypatch.setattr(adapter, "_chat", fake_chat)

    config = adapter.generate_daily_config(MorningInput(date=date(2026, 4, 28), watchlist=["AAPL"], vix=18.0))

    assert config.provider == ProviderName.QWEN
    assert config.model == "Qwen/Qwen3-8B"
    assert config.trading_enabled is False
    assert "required" in captured["system"]
    assert captured["response_format"] == {"type": "json_object"}
