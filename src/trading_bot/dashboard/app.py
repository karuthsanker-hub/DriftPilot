from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from datetime import date

from pydantic import BaseModel, SecretStr

from trading_bot.config import EnvConfigStore
from trading_bot.data.market_data import YFinanceMarketDataProvider
from trading_bot.data.repositories import StrategyConfigRepository, TradingRepository
from trading_bot.data.supabase_client import create_supabase_client
from trading_bot.diagnostics import run_env_diagnostics
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_engine import PaperExecutionEngine
from trading_bot.llm.factory import adapter_for_provider
from trading_bot.llm.models import ProviderName, ProviderSettings, ProviderStatus
from trading_bot.scanners.pead_scanner import PEADScanner
from trading_bot.scheduler import TradingSchedulerService
from trading_bot.sentiment import FinBERTSentimentScorer, KeywordSentimentScorer
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


class PEADScanRequest(BaseModel):
    tickers: str
    scan_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    persist: bool = True
    persist_skips: bool = False
    sentiment: str = "keyword"


class ExecutePendingRequest(BaseModel):
    submit: bool = False


def create_app(env_path: Path | str = ".env") -> FastAPI:
    app = FastAPI(title="AI Trading Bot Dashboard")
    store = EnvConfigStore(env_path)
    scheduler_service = TradingSchedulerService(env_path=str(env_path))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "dashboard.html")

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request):
        return templates.TemplateResponse(request, "admin.html")

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

    @app.get("/api/diagnostics")
    def diagnostics():
        try:
            return [_object_dict(result) for result in run_env_diagnostics(env_path, network=True)]
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/providers/health")
    def provider_health_all():
        settings = store.settings()
        results = []
        for provider in ProviderName:
            try:
                results.append(adapter_for_provider(provider, settings).health_check().model_dump(mode="json"))
            except Exception as exc:
                results.append({"provider": provider.value, "configured": False, "ok": False, "message": str(exc)})
        return results

    @app.post("/api/scan-pead")
    def scan_pead(payload: PEADScanRequest):
        try:
            settings = load_settings(env_path)
            repository = TradingRepository(create_supabase_client(settings)) if payload.persist else None
            scorer = FinBERTSentimentScorer() if payload.sentiment == "finbert" else KeywordSentimentScorer()
            scanner = PEADScanner(YFinanceMarketDataProvider(), scorer, repository)
            tickers = [ticker.strip() for ticker in payload.tickers.split(",") if ticker.strip()]
            dates = _scan_dates(payload)
            results = []
            for scan_date in dates:
                for result in scanner.scan(tickers, scan_date, persist_skips=payload.persist_skips):
                    results.append((scan_date, result))
            return [
                {
                    "scan_date": scan_date.isoformat(),
                    "ticker": result.ticker,
                    "action": result.signal.action.value,
                    "surprise_pct": result.signal.surprise_pct,
                    "skip_reason": result.signal.skip_reason,
                    "persisted": result.persisted,
                }
                for scan_date, result in results
            ]
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/execute-pending")
    def execute_pending(payload: ExecutePendingRequest):
        try:
            settings = load_settings(env_path)
            client = create_supabase_client(settings)
            engine = PaperExecutionEngine(
                TradingRepository(client),
                StrategyConfigRepository(client),
                AlpacaBroker(settings),
            )
            summary = engine.execute_pending_watchlist(dry_run=not payload.submit)
            return _object_dict(summary)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/scheduler")
    def scheduler_status():
        return scheduler_service.status()

    @app.post("/api/scheduler/start")
    def scheduler_start():
        return scheduler_service.start().__dict__

    @app.post("/api/scheduler/stop")
    def scheduler_stop():
        return scheduler_service.stop().__dict__

    @app.post("/api/scheduler/run/{job_id}")
    def scheduler_run(job_id: str):
        try:
            if job_id == "daily_pead_scan":
                return scheduler_service.run_pead_scan()
            if job_id == "pending_entry_scan":
                return scheduler_service.run_pending_entries()
            if job_id == "weekly_momentum_scan":
                return scheduler_service.run_momentum_scan()
            raise HTTPException(status_code=404, detail=f"Unknown scheduler job: {job_id}")
        except HTTPException:
            raise
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


def _object_dict(value) -> dict:
    if hasattr(value, "__dict__") and value.__dict__:
        return dict(value.__dict__)
    return {
        key: getattr(value, key)
        for key in dir(value)
        if not key.startswith("_") and not callable(getattr(value, key))
    }


def _scan_dates(payload: PEADScanRequest) -> list[date]:
    if payload.start_date or payload.end_date:
        start = payload.start_date or payload.end_date
        end = payload.end_date or payload.start_date
        if start is None or end is None:
            return [date.today()]
        if start > end:
            start, end = end, start
        days = (end - start).days
        if days > 31:
            raise HTTPException(status_code=400, detail="Date range scans are capped at 31 days")
        return [start.fromordinal(start.toordinal() + offset) for offset in range(days + 1)]
    return [payload.scan_date or date.today()]


app = create_app()
