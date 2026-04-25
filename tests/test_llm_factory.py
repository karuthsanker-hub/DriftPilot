from __future__ import annotations

from trading_bot.llm.factory import adapter_from_settings, adapter_for_provider
from trading_bot.llm.models import ProviderName, ProviderSettings
from trading_bot.llm.providers.claude_adapter import ClaudeAdapter
from trading_bot.llm.providers.gemini_adapter import GeminiAdapter
from trading_bot.llm.providers.openai_adapter import OpenAIAdapter
from trading_bot.llm.providers.qwen_adapter import QwenAdapter


def test_factory_returns_native_adapter_for_each_provider() -> None:
    settings = ProviderSettings()

    assert isinstance(adapter_for_provider(ProviderName.OPENAI, settings), OpenAIAdapter)
    assert isinstance(adapter_for_provider(ProviderName.CLAUDE, settings), ClaudeAdapter)
    assert isinstance(adapter_for_provider(ProviderName.GEMINI, settings), GeminiAdapter)
    assert isinstance(adapter_for_provider(ProviderName.QWEN, settings), QwenAdapter)


def test_active_provider_controls_selected_adapter() -> None:
    settings = ProviderSettings(active_provider=ProviderName.QWEN)

    assert isinstance(adapter_from_settings(settings), QwenAdapter)

