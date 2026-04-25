from __future__ import annotations

import json
import os
from typing import Any

from trading_bot.llm.base import LLMAdapter, LLMConfigurationError, LLMOutputValidationError
from trading_bot.llm.models import (
    CostEstimate,
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


class OpenAIAdapter(LLMAdapter):
    provider = ProviderName.OPENAI

    def __init__(self, model: str = "gpt-4.1", api_key: str | None = None) -> None:
        self.model = model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")

    def _client(self):
        if not self.api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is not configured")
        from openai import OpenAI

        return OpenAI(api_key=self.api_key)

    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        response = self._client().responses.create(
            model=self.model,
            instructions=MORNING_SYSTEM_PROMPT,
            input=morning_user_prompt(payload),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "daily_config",
                    "strict": True,
                    "schema": daily_config_schema(),
                }
            },
        )
        return self._validate_daily_config(_response_json(response))

    def generate_evening_review(self, payload: EveningInput) -> LearningLog:
        response = self._client().responses.create(
            model=self.model,
            instructions=EVENING_SYSTEM_PROMPT,
            input=evening_user_prompt(payload),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "learning_log",
                    "strict": True,
                    "schema": learning_log_schema(),
                }
            },
        )
        return self._validate_learning_log(_response_json(response))

    def health_check(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(provider=self.provider, model=self.model, configured=False, ok=False, message="Missing OPENAI_API_KEY")
        try:
            self._client().responses.create(model=self.model, input="Return the word ok.", max_output_tokens=5)
            return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=True, message="OpenAI connection OK")
        except Exception as exc:
            return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=False, message=str(exc))

    def estimate_cost(self, payload: MorningInput | EveningInput) -> CostEstimate | None:
        text = payload.model_dump_json()
        return CostEstimate(
            provider=self.provider,
            model=self.model,
            estimated_input_tokens=max(1, len(text) // 4),
            estimated_output_tokens=1_500,
            estimated_cost_usd=None,
        )


def _response_json(response: Any) -> dict[str, Any]:
    output_text = getattr(response, "output_text", None)
    if not output_text:
        raise LLMOutputValidationError("OpenAI response did not include output_text")
    try:
        return json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise LLMOutputValidationError(f"OpenAI response was not valid JSON: {exc}") from exc

