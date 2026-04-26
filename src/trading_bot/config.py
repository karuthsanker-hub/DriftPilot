from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from trading_bot.llm.models import ProviderName, ProviderSettings


DEFAULT_ENV_PATH = Path(".env")


class EnvConfigStore:
    """Small .env manager used by the local dashboard."""

    def __init__(self, env_path: Path | str = DEFAULT_ENV_PATH) -> None:
        self.env_path = Path(env_path)
        load_dotenv(self.env_path, override=False)

    def read_raw(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if self.env_path.exists():
            for line in self.env_path.read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip()
        return values

    def write_values(self, updates: dict[str, str]) -> None:
        current = self.read_raw()
        current.update({key: value for key, value in updates.items() if value is not None})
        lines = [f"{key}={value}" for key, value in sorted(current.items())]
        self.env_path.write_text("\n".join(lines) + "\n")
        for key, value in current.items():
            os.environ[key] = value

    def settings(self) -> ProviderSettings:
        raw = self.read_raw()
        getenv = lambda key, default="": raw.get(key) or os.getenv(key, default)
        return ProviderSettings(
            active_provider=ProviderName(getenv("ACTIVE_LLM_PROVIDER", ProviderName.OPENAI.value)),
            openai_model=getenv("OPENAI_MODEL", "gpt-4.1"),
            claude_model=getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            gemini_model=getenv("GEMINI_MODEL", "gemini-2.5-pro"),
            qwen_base_url=getenv("QWEN_BASE_URL", "http://localhost:8001/v1"),
            qwen_model=getenv("QWEN_MODEL", "qwen2.5-coder"),
            openai_configured=bool(getenv("OPENAI_API_KEY")),
            anthropic_configured=bool(getenv("ANTHROPIC_API_KEY")),
            gemini_configured=bool(getenv("GEMINI_API_KEY")),
            qwen_configured=bool(getenv("QWEN_BASE_URL")),
        )

    def save_settings(
        self,
        *,
        active_provider: ProviderName,
        openai_model: str,
        claude_model: str,
        gemini_model: str,
        qwen_base_url: str,
        qwen_model: str,
        qwen_api_key: str = "",
        openai_api_key: str = "",
        anthropic_api_key: str = "",
        gemini_api_key: str = "",
    ) -> ProviderSettings:
        updates = {
            "ACTIVE_LLM_PROVIDER": active_provider.value,
            "OPENAI_MODEL": openai_model,
            "CLAUDE_MODEL": claude_model,
            "GEMINI_MODEL": gemini_model,
            "QWEN_BASE_URL": qwen_base_url.rstrip("/"),
            "QWEN_MODEL": qwen_model,
        }
        if openai_api_key:
            updates["OPENAI_API_KEY"] = openai_api_key
        if anthropic_api_key:
            updates["ANTHROPIC_API_KEY"] = anthropic_api_key
        if gemini_api_key:
            updates["GEMINI_API_KEY"] = gemini_api_key
        if qwen_api_key:
            updates["QWEN_API_KEY"] = qwen_api_key
        self.write_values(updates)
        return self.settings()
