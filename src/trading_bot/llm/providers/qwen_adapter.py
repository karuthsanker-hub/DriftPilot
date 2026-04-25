from __future__ import annotations

import json
import os
from typing import Any

import httpx

from trading_bot.llm.base import LLMAdapter, LLMConfigurationError, LLMOutputValidationError
from trading_bot.llm.models import DailyConfig, EveningInput, LearningLog, MorningInput, ProviderName, ProviderStatus
from trading_bot.llm.prompts import (
    EVENING_SYSTEM_PROMPT,
    MORNING_SYSTEM_PROMPT,
    evening_user_prompt,
    morning_user_prompt,
)


class QwenAdapter(LLMAdapter):
    provider = ProviderName.QWEN

    def __init__(
        self,
        model: str = "qwen2.5-coder",
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.getenv("QWEN_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("QWEN_API_KEY", "")
        self.timeout = timeout

    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        raw = self._chat_json(MORNING_SYSTEM_PROMPT, morning_user_prompt(payload))
        return self._validate_daily_config(raw)

    def generate_evening_review(self, payload: EveningInput) -> LearningLog:
        raw = self._chat_json(EVENING_SYSTEM_PROMPT, evening_user_prompt(payload))
        return self._validate_learning_log(raw)

    def health_check(self) -> ProviderStatus:
        if not self.base_url:
            return ProviderStatus(provider=self.provider, model=self.model, configured=False, ok=False, message="Missing QWEN_BASE_URL")
        try:
            self._chat("Return only ok.", "health check", max_tokens=5)
            return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=True, message="Qwen endpoint OK")
        except Exception as exc:
            return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=False, message=str(exc))

    def _chat_json(self, system: str, prompt: str) -> dict[str, Any]:
        content = self._chat(
            system + "\nReturn valid JSON only. Do not wrap the JSON in Markdown.",
            prompt,
            max_tokens=4_000,
            response_format={"type": "json_object"},
        )
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMOutputValidationError(f"Qwen response was not valid JSON: {exc}") from exc

    def _chat(self, system: str, user: str, *, max_tokens: int, response_format: dict[str, str] | None = None) -> str:
        if not self.base_url:
            raise LLMConfigurationError("QWEN_BASE_URL is not configured")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        response = httpx.post(f"{self.base_url}/chat/completions", headers=headers, json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMOutputValidationError("Qwen endpoint returned an unexpected response shape") from exc

