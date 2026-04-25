from __future__ import annotations

import os

from trading_bot.config import EnvConfigStore
from trading_bot.llm.base import LLMAdapter
from trading_bot.llm.models import ProviderName, ProviderSettings
from trading_bot.llm.providers.claude_adapter import ClaudeAdapter
from trading_bot.llm.providers.gemini_adapter import GeminiAdapter
from trading_bot.llm.providers.openai_adapter import OpenAIAdapter
from trading_bot.llm.providers.qwen_adapter import QwenAdapter


def adapter_from_settings(settings: ProviderSettings) -> LLMAdapter:
    provider = settings.active_provider
    if provider == ProviderName.OPENAI:
        return OpenAIAdapter(model=settings.openai_model)
    if provider == ProviderName.CLAUDE:
        return ClaudeAdapter(model=settings.claude_model)
    if provider == ProviderName.GEMINI:
        return GeminiAdapter(model=settings.gemini_model)
    if provider == ProviderName.QWEN:
        return QwenAdapter(model=settings.qwen_model, base_url=settings.qwen_base_url)
    raise ValueError(f"Unsupported provider: {provider}")


def adapter_for_provider(provider: ProviderName, settings: ProviderSettings | None = None) -> LLMAdapter:
    settings = settings or EnvConfigStore().settings()
    selected = settings.model_copy(update={"active_provider": provider})
    return adapter_from_settings(selected)


def active_adapter(env_path: str | None = None) -> LLMAdapter:
    store = EnvConfigStore(env_path) if env_path else EnvConfigStore()
    return adapter_from_settings(store.settings())


def configured_provider_name() -> ProviderName:
    return ProviderName(os.getenv("ACTIVE_LLM_PROVIDER", ProviderName.OPENAI.value))

