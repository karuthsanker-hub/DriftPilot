from __future__ import annotations

import json
import os
from typing import Any

from trading_bot.llm.base import LLMAdapter, LLMConfigurationError, LLMOutputValidationError
from trading_bot.llm.models import (
    DailyConfig,
    EveningInput,
    LearningLog,
    MorningInput,
    ProviderName,
    ProviderStatus,
    daily_config_schema,
    learning_log_schema,
)
from trading_bot.llm.prompts import (
    EVENING_SYSTEM_PROMPT,
    MORNING_SYSTEM_PROMPT,
    evening_user_prompt,
    morning_user_prompt,
)


class GeminiAdapter(LLMAdapter):
    provider = ProviderName.GEMINI

    def __init__(self, model: str = "gemini-2.5-pro", api_key: str | None = None) -> None:
        self.model = model
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")

    def _client(self):
        if not self.api_key:
            raise LLMConfigurationError("GEMINI_API_KEY is not configured")
        from google import genai

        return genai.Client(api_key=self.api_key)

    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        raw = self._generate_json(MORNING_SYSTEM_PROMPT, morning_user_prompt(payload), daily_config_schema())
        return self._validate_daily_config(raw)

    def generate_evening_review(self, payload: EveningInput) -> LearningLog:
        raw = self._generate_json(EVENING_SYSTEM_PROMPT, evening_user_prompt(payload), learning_log_schema())
        return self._validate_learning_log(raw)

    def health_check(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(provider=self.provider, model=self.model, configured=False, ok=False, message="Missing GEMINI_API_KEY")
        try:
            self._client().models.generate_content(model=self.model, contents="Return only ok.")
            return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=True, message="Gemini connection OK")
        except Exception as exc:
            return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=False, message=str(exc))

    def _generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        from google.genai import types

        response = self._client().models.generate_content(
            model=self.model,
            contents=f"{system}\n\n{prompt}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise LLMOutputValidationError(f"Gemini response was not valid JSON: {exc}") from exc

