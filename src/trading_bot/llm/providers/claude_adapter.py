from __future__ import annotations

import os
from typing import Any

from trading_bot.llm.base import LLMAdapter, LLMConfigurationError, LLMOutputValidationError
from trading_bot.llm.models import DailyConfig, EveningInput, LearningLog, MorningInput, ProviderName, ProviderStatus
from trading_bot.llm.prompts import (
    EVENING_SYSTEM_PROMPT,
    MORNING_SYSTEM_PROMPT,
    evening_user_prompt,
    morning_user_prompt,
)


class ClaudeAdapter(LLMAdapter):
    provider = ProviderName.CLAUDE

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = (base_url or os.getenv("CLAUDE_BASE_URL", "https://api.anthropic.com")).rstrip("/")

    def _client(self):
        if not self.api_key:
            raise LLMConfigurationError("ANTHROPIC_API_KEY is not configured")
        from anthropic import Anthropic

        return Anthropic(api_key=self.api_key, base_url=self.base_url)

    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        raw = self._tool_json(
            name="emit_daily_config",
            description="Emit a validated paper-trading daily config.",
            prompt=morning_user_prompt(payload),
            system=MORNING_SYSTEM_PROMPT,
        )
        return self._validate_daily_config(raw)

    def generate_evening_review(self, payload: EveningInput) -> LearningLog:
        raw = self._tool_json(
            name="emit_learning_log",
            description="Emit a validated paper-trading evening learning log.",
            prompt=evening_user_prompt(payload),
            system=EVENING_SYSTEM_PROMPT,
        )
        return self._validate_learning_log(raw)

    def health_check(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(provider=self.provider, model=self.model, configured=False, ok=False, message="Missing ANTHROPIC_API_KEY")
        try:
            self._client().messages.create(
                model=self.model,
                max_tokens=16,
                system="Return only ok.",
                messages=[{"role": "user", "content": "health check"}],
            )
            return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=True, message="Claude connection OK")
        except Exception as exc:
            return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=False, message=str(exc))

    def _tool_json(self, *, name: str, description: str, prompt: str, system: str) -> dict[str, Any]:
        response = self._client().messages.create(
            model=self.model,
            max_tokens=4_000,
            system=system,
            tools=[
                {
                    "name": name,
                    "description": description,
                    "input_schema": {"type": "object", "additionalProperties": True},
                }
            ],
            tool_choice={"type": "tool", "name": name},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == name:
                return dict(block.input)
        raise LLMOutputValidationError("Claude response did not include the expected tool payload")
