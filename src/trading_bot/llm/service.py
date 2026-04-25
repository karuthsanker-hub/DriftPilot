from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from trading_bot.llm.base import LLMAdapter, LLMAdapterError
from trading_bot.llm.models import DailyConfig, EveningInput, LearningLog, MorningInput

logger = logging.getLogger(__name__)


class StrategyLLMService:
    """Retry/fallback wrapper around a provider adapter."""

    def __init__(
        self,
        adapter: LLMAdapter,
        *,
        daily_config_path: Path | str = "config/daily_config.json",
        learning_log_path: Path | str = "config/learning_log.json",
    ) -> None:
        self.adapter = adapter
        self.daily_config_path = Path(daily_config_path)
        self.learning_log_path = Path(learning_log_path)

    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        try:
            config = self._retry(lambda: self.adapter.generate_daily_config(payload))
            self._write_json(self.daily_config_path, config.model_dump(mode="json"))
            return config
        except LLMAdapterError as exc:
            fallback = self._load_previous_daily_config()
            logger.warning(
                "LLM daily config failed; using previous config",
                extra={"provider": self.adapter.provider, "model": self.adapter.model, "error": str(exc)},
            )
            return fallback

    def generate_evening_review(self, payload: EveningInput) -> LearningLog:
        review = self._retry(lambda: self.adapter.generate_evening_review(payload))
        self._write_json(self.learning_log_path, review.model_dump(mode="json"))
        return review

    def _retry(self, call):
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                return call()
            except Exception as exc:
                last_exc = exc
        assert last_exc is not None
        if isinstance(last_exc, LLMAdapterError):
            raise last_exc
        raise LLMAdapterError(str(last_exc)) from last_exc

    def _load_previous_daily_config(self) -> DailyConfig:
        if not self.daily_config_path.exists():
            raise LLMAdapterError("No previous daily config is available for fallback")
        try:
            return DailyConfig.model_validate_json(self.daily_config_path.read_text())
        except (ValidationError, json.JSONDecodeError) as exc:
            raise LLMAdapterError("Previous daily config is missing or invalid") from exc

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
