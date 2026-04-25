from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import ValidationError

from trading_bot.llm.models import CostEstimate, DailyConfig, EveningInput, LearningLog, MorningInput, ProviderStatus


class LLMAdapterError(RuntimeError):
    """Base error for provider adapter failures."""


class LLMConfigurationError(LLMAdapterError):
    """Raised when a provider is selected but not configured."""


class LLMOutputValidationError(LLMAdapterError):
    """Raised when provider output does not satisfy local contracts."""


class LLMAdapter(ABC):
    provider: str
    model: str

    @abstractmethod
    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        raise NotImplementedError

    @abstractmethod
    def generate_evening_review(self, payload: EveningInput) -> LearningLog:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> ProviderStatus:
        raise NotImplementedError

    def estimate_cost(self, payload: MorningInput | EveningInput) -> CostEstimate | None:
        return None

    def _validate_daily_config(self, raw: dict[str, Any]) -> DailyConfig:
        raw = self._with_provider_metadata(raw)
        try:
            return DailyConfig.model_validate(raw)
        except ValidationError as exc:
            raise LLMOutputValidationError(str(exc)) from exc

    def _validate_learning_log(self, raw: dict[str, Any]) -> LearningLog:
        raw = self._with_provider_metadata(raw)
        try:
            return LearningLog.model_validate(raw)
        except ValidationError as exc:
            raise LLMOutputValidationError(str(exc)) from exc

    def _with_provider_metadata(self, raw: dict[str, Any]) -> dict[str, Any]:
        stamped = dict(raw)
        stamped["provider"] = self.provider
        stamped["model"] = self.model
        return stamped
