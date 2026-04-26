from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, SecretStr

from trading_bot.config import EnvConfigStore
from trading_bot.data.repositories import StrategyConfigRepository, TradingRepository
from trading_bot.data.supabase_client import create_supabase_client
from trading_bot.llm.factory import adapter_for_provider
from trading_bot.llm.models import ProviderName, ProviderSettings, ProviderStatus
from trading_bot.settings import load_settings


TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


class ProviderSettingsUpdate(BaseModel):
    active_provider: ProviderName
    openai_model: str
    claude_model: str
    gemini_model: str
    qwen_base_url: str
    qwen_model: str
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    qwen_api_key: SecretStr | None = None


def create_app(env_path: Path | str = ".env") -> FastAPI:
    app = FastAPI(title="AI Trading Bot Dashboard")
    store = EnvConfigStore(env_path)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "dashboard.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/llm", response_class=HTMLResponse)
    def llm_settings(request: Request):
        return templates.TemplateResponse(request, "settings.html")

    @app.get("/settings", response_model=ProviderSettings)
    def get_settings() -> ProviderSettings:
        return store.settings()

    @app.post("/settings", response_model=ProviderSettings)
    def save_settings(update: ProviderSettingsUpdate) -> ProviderSettings:
        return store.save_settings(
            active_provider=update.active_provider,
            openai_model=update.openai_model,
            claude_model=update.claude_model,
            gemini_model=update.gemini_model,
            qwen_base_url=update.qwen_base_url,
            qwen_model=update.qwen_model,
            openai_api_key=_secret(update.openai_api_key),
            anthropic_api_key=_secret(update.anthropic_api_key),
            gemini_api_key=_secret(update.gemini_api_key),
            qwen_api_key=_secret(update.qwen_api_key),
        )

    @app.get("/settings/providers/{provider}/health", response_model=ProviderStatus)
    def provider_health(provider: ProviderName) -> ProviderStatus:
        settings = store.settings()
        try:
            adapter = adapter_for_provider(provider, settings)
            return adapter.health_check()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/watchlist")
    def watchlist():
        try:
            repo = TradingRepository(create_supabase_client(load_settings(env_path)))
            return repo.list_recent_watchlist(limit=50)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/strategy-config")
    def strategy_config():
        try:
            repo = StrategyConfigRepository(create_supabase_client(load_settings(env_path)))
            return repo.list_config()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/kill-switch")
    def set_kill_switch(active: bool):
        try:
            repo = StrategyConfigRepository(create_supabase_client(load_settings(env_path)))
            repo.set_trading_active(active)
            return {"trading_active": active}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return app


def _secret(value: SecretStr | None) -> str:
    if value is None:
        return ""
    secret = value.get_secret_value()
    return secret.strip()


app = create_app()
