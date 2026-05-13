from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from datetime import date

from pydantic import BaseModel, SecretStr

from driftpilot.dashboard.agent_views import agent_dashboard_payload, agent_decision_detail
from driftpilot.dashboard.view_models import admin_state_payload, backtest_report_payload, diagnostics_payload, operator_state_payload
from driftpilot.settings import load_settings as load_driftpilot_settings
from driftpilot.storage.repositories import DriftPilotRepository
from trading_bot.backtesting import BacktestTrade, run_backtest, run_split_backtest
from trading_bot.config import EnvConfigStore
from trading_bot.data.earnings_events import EarningsEventStore
from trading_bot.data.macro_data import FredMacroDataProvider
from trading_bot.data.provider_factory import create_market_data_provider
from trading_bot.data.repositories import StrategyConfigRepository, TradingRepository
from trading_bot.data.supabase_client import create_supabase_client
from trading_bot.diagnostics import run_env_diagnostics
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_engine import PaperExecutionEngine
from trading_bot.llm.factory import active_adapter, adapter_for_provider
from trading_bot.llm.models import EveningInput, ProviderName, ProviderSettings, ProviderStatus
from trading_bot.operator import approve_paper_trades, build_top_bets, momentum_rows_to_operator_rows
from trading_bot.scanners.pead_scanner import PEADScanner
from trading_bot.scheduler import TradingSchedulerService
from trading_bot.sentiment import FinBERTSentimentScorer, KeywordSentimentScorer
from trading_bot.settings import load_settings
from trading_bot.strategies.risk import evaluate_daily_pause
from trading_bot.universe import load_pead_universe


TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
logger = logging.getLogger("trading_bot.dashboard")


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
    tickers: str = ""
    scan_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    persist: bool = True
    persist_skips: bool = False
    sentiment: str = "keyword"


class ExecutePendingRequest(BaseModel):
    submit: bool = False


class BacktestRequest(BaseModel):
    starting_equity: float = 50_000
    transaction_cost_bps: float = 5
    spy_return_pct: float | None = None
    split: bool = False


class ApprovePaperTradesRequest(BaseModel):
    selected_ids: list[str]
    submit: bool = False


class ResetPaperStateRequest(BaseModel):
    reset_broker: bool = True


class OperatorSettingsUpdate(BaseModel):
    paper_capital: float
    target_pct: float
    stop_pct: float
    max_candidates: int
    trade_slots: int
    min_candidates: int
    refresh_interval_minutes: int
    universe_refresh_interval_minutes: int = 5
    monitor_interval_minutes: int = 5


def create_app(env_path: Path | str = ".env") -> FastAPI:
    store = EnvConfigStore(env_path)
    scheduler_service = TradingSchedulerService(env_path=str(env_path))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler_service.start()
        try:
            yield
        finally:
            scheduler_service.stop()

    app = FastAPI(title="AI Trading Bot Dashboard", lifespan=lifespan)

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                extra={"request_id": request_id, "method": request.method, "path": request.url.path},
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "request_id": request_id},
                headers={"x-request-id": request_id},
            )
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["x-request-id"] = request_id
        log_method = logger.warning if response.status_code >= 400 else logger.info
        log_method(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        logger.warning(
            "request_validation_failed",
            extra={"request_id": request_id, "method": request.method, "path": request.url.path, "errors": exc.errors()},
        )
        return JSONResponse(
            status_code=422,
            content={"detail": "Request validation failed", "errors": exc.errors(), "request_id": request_id},
            headers={"x-request-id": request_id},
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "dashboard.html")

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request):
        return templates.TemplateResponse(request, "admin.html")

    @app.get("/backtest", response_class=HTMLResponse)
    def backtest_page(request: Request):
        return templates.TemplateResponse(request, "backtest.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "driftpilot-dashboard", "scheduler_running": scheduler_service.status()["running"]}

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
            _raise_api_error(exc, "provider_health", status_code=400)

    @app.get("/api/watchlist")
    def watchlist():
        try:
            repo = TradingRepository(create_supabase_client(load_settings(env_path)))
            return repo.list_recent_watchlist(limit=50)
        except Exception as exc:
            _raise_api_error(exc, "watchlist")

    @app.get("/api/momentum-scores")
    def momentum_scores():
        try:
            repo = TradingRepository(create_supabase_client(load_settings(env_path)))
            return repo.list_recent_momentum_scores(limit=50)
        except Exception as exc:
            _raise_api_error(exc, "momentum_scores")

    @app.get("/api/trades")
    def trades():
        try:
            repo = TradingRepository(create_supabase_client(load_settings(env_path)))
            return repo.list_recent_trades(limit=50)
        except Exception as exc:
            _raise_api_error(exc, "trades")

    @app.get("/api/daily-summaries")
    def daily_summaries():
        try:
            repo = TradingRepository(create_supabase_client(load_settings(env_path)))
            return repo.list_daily_summaries(limit=20)
        except Exception as exc:
            _raise_api_error(exc, "daily_summaries")

    @app.post("/api/backtest/trades")
    def backtest_trades(payload: BacktestRequest):
        try:
            repo = TradingRepository(create_supabase_client(load_settings(env_path)))
            rows = repo.list_recent_trades(limit=500)
            trades = [
                BacktestTrade(
                    ticker=row["ticker"],
                    side=row.get("side") or "buy",
                    entry_price=float(row["entry_price"]),
                    exit_price=float(row["exit_price"]),
                    shares=int(row["shares"]),
                )
                for row in rows
                if row.get("entry_price") is not None and row.get("exit_price") is not None and row.get("shares") is not None
            ]
            if payload.split:
                result: Any = run_split_backtest(
                    trades,
                    starting_equity=payload.starting_equity,
                    transaction_cost_bps=payload.transaction_cost_bps,
                )
            else:
                result = run_backtest(
                    trades,
                    starting_equity=payload.starting_equity,
                    transaction_cost_bps=payload.transaction_cost_bps,
                    spy_return_pct=payload.spy_return_pct,
                )
            return asdict(result)
        except Exception as exc:
            _raise_api_error(exc, "backtest_trades")

    @app.get("/api/strategy-config")
    def strategy_config():
        try:
            repo = StrategyConfigRepository(create_supabase_client(load_settings(env_path)))
            return repo.list_config()
        except Exception as exc:
            _raise_api_error(exc, "strategy_config")

    @app.get("/api/diagnostics")
    def diagnostics():
        try:
            return [_object_dict(result) for result in run_env_diagnostics(env_path, network=True)]
        except Exception as exc:
            _raise_api_error(exc, "diagnostics")

    @app.get("/api/providers/health")
    def provider_health_all():
        settings = store.settings()
        results = []
        for provider in ProviderName:
            try:
                results.append(adapter_for_provider(provider, settings).health_check().model_dump(mode="json"))
            except Exception as exc:
                logger.warning("provider_health_failed", extra={"provider": provider.value, "error": _safe_error(exc)})
                results.append({"provider": provider.value, "configured": False, "ok": False, "message": _safe_error(exc)})
        return results

    @app.get("/api/operator/settings")
    def operator_settings():
        settings = load_settings(env_path)
        return _operator_settings_payload(settings)

    @app.get("/api/operator/state")
    def operator_state():
        try:
            return operator_state_payload(load_driftpilot_settings(env_path))
        except Exception as exc:
            _raise_api_error(exc, "operator_state")

    @app.get("/api/backtest/report")
    def backtest_report():
        try:
            return backtest_report_payload()
        except Exception as exc:
            _raise_api_error(exc, "backtest_report")

    @app.get("/api/operator/news-ticker")
    def operator_news_ticker(limit: int = 30, lookback_minutes: int = 240):
        """Recent catalyst events for the dashboard scrolling ticker."""
        try:
            from driftpilot.dashboard.view_models import _news_ticker
            return {"events": _news_ticker(limit=limit, lookback_minutes=lookback_minutes)}
        except Exception as exc:
            _raise_api_error(exc, "operator_news_ticker")

    @app.get("/api/catalyst/event/{event_id}")
    def catalyst_event_detail(event_id: int):
        """Full catalyst event detail for the enrichment audit panel."""
        try:
            from driftpilot.dashboard.view_models import _catalyst_detail
            return _catalyst_detail(event_id)
        except Exception as exc:
            _raise_api_error(exc, "catalyst_event_detail")

    @app.get("/api/operator/diagnostics")
    def operator_diagnostics():
        """Operator diagnostics: catalyst pool, per-symbol P&L, slot analysis,
        rejection pipeline, signal breakdown. Surfaces data that previously
        required CLI forensics."""
        try:
            return diagnostics_payload(load_driftpilot_settings(env_path))
        except Exception as exc:
            _raise_api_error(exc, "operator_diagnostics")

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request):
        return templates.TemplateResponse(request, "agents.html")

    @app.get("/api/agents/dashboard")
    def agents_dashboard():
        """Full agent dashboard payload — states, decisions, override rate."""
        try:
            dp_settings = load_driftpilot_settings(env_path)
            return agent_dashboard_payload(dp_settings.agent_db_path)
        except Exception as exc:
            _raise_api_error(exc, "agents_dashboard")

    @app.get("/api/agents/decision/{decision_id}")
    def agents_decision(decision_id: int):
        """Full detail for one agent decision (drill-down)."""
        try:
            dp_settings = load_driftpilot_settings(env_path)
            return agent_decision_detail(decision_id, dp_settings.agent_db_path)
        except Exception as exc:
            _raise_api_error(exc, "agents_decision")

    @app.get("/api/agents/export/stats")
    def agents_export_stats():
        """Training data export statistics."""
        try:
            from driftpilot.agents.training_exporter import ExportFilters, TrainingExporter
            dp_settings = load_driftpilot_settings(env_path)
            exporter = TrainingExporter(dp_settings.agent_db_path)
            stats = exporter.get_stats(ExportFilters())
            return {
                "total_decisions": stats.total_decisions,
                "overrides": stats.overrides,
                "override_rate": stats.override_rate,
                "outcomes_filled": stats.outcomes_filled,
                "accuracy": stats.accuracy,
                "avg_latency_ms": stats.avg_latency_ms,
                "models_used": stats.models_used,
                "decision_types": stats.decision_types,
                "agents": stats.agents,
            }
        except FileNotFoundError:
            return {"total_decisions": 0, "error": "Agent database not found"}
        except Exception as exc:
            _raise_api_error(exc, "agents_export_stats")

    @app.get("/api/admin/state")
    def admin_state():
        try:
            return admin_state_payload(load_driftpilot_settings(env_path))
        except Exception as exc:
            _raise_api_error(exc, "admin_state")

    @app.get("/api/admin/runtime-config")
    def get_runtime_config():
        try:
            from driftpilot.runtime_config import field_specs, load_runtime_config
            cfg = load_runtime_config()
            return {"values": cfg.to_dict(), "fields": field_specs()}
        except Exception as exc:
            _raise_api_error(exc, "get_runtime_config")

    @app.post("/api/admin/runtime-config")
    async def post_runtime_config(payload: dict):
        try:
            from driftpilot.runtime_config import save_runtime_config
            cfg = save_runtime_config(payload)
            return {
                "ok": True,
                "values": cfg.to_dict(),
                "note": (
                    "Signal config (max_age/profit/stop/hold/sentiment) "
                    "hot-reloads on next scan cycle (~30s). slot_value and "
                    "max_trades_per_symbol_per_day require operator restart."
                ),
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            _raise_api_error(exc, "post_runtime_config")

    @app.post("/api/admin/override/{action}")
    def admin_override(action: str):
        allowed = {
            "pause_scanning": "manual_pause_scanning",
            "resume_scanning": "manual_resume_scanning",
            "flat_all_positions": "manual_flat_all_positions",
            "reset_paper_state": "manual_reset_paper_state",
            "cancel_open_orders": "manual_cancel_open_orders",
            "force_reconciliation": "manual_force_reconciliation",
            "restart_data_stream": "manual_restart_data_stream",
        }
        if action not in allowed:
            raise HTTPException(status_code=404, detail=f"Unknown override action: {action}")
        try:
            settings = load_driftpilot_settings(env_path)
            repo = DriftPilotRepository.open(settings.sqlite_path_obj)
            now = repo.clock.now_utc()
            if action in {"flat_all_positions", "reset_paper_state"}:
                for position in repo.positions.list_open():
                    repo.positions.close(
                        position.id,
                        exit_reason=allowed[action],
                        realized_pnl=0.0,
                        closed_at=now,
                        metadata={"manual_override": action},
                    )
                for slot in repo.slots.list_all():
                    repo.slots.upsert(
                        slot.slot_id,
                        status="EMPTY",
                        symbol=None,
                        position_id=None,
                        reserved_order_id=None,
                        slot_value=slot.slot_value,
                        metadata={"empty_reason": "Manual override"},
                        updated_at=now,
                    )
            current = repo.state.get()
            transition = repo.transitions.append(
                from_state=current.current_state if current else None,
                to_state="BOOT" if action in {"resume_scanning", "force_reconciliation"} else "HALTED_RISK",
                reason=allowed[action],
                metadata={"manual_override": action},
                timestamp=now,
            )
            repo.state.set(
                transition.to_state,
                last_transition_id=transition.id,
                metadata={"manual_override": action},
                updated_at=now,
            )
            return {"ok": True, "action": action, "transition_id": transition.id}
        except Exception as exc:
            _raise_api_error(exc, "admin_override")

    @app.post("/api/operator/settings")
    def save_operator_settings(update: OperatorSettingsUpdate):
        if update.paper_capital <= 0:
            raise HTTPException(status_code=400, detail="Paper capital must be greater than zero")
        if not 0 < update.target_pct <= 0.25 or not 0 < update.stop_pct <= 0.25:
            raise HTTPException(status_code=400, detail="Target and stop percentages must be between 0 and 25%")
        if not 1 <= update.max_candidates <= 100:
            raise HTTPException(status_code=400, detail="Candidate limit must be between 1 and 100")
        if not 1 <= update.trade_slots <= 100:
            raise HTTPException(status_code=400, detail="Trade slots must be between 1 and 100")
        if not 1 <= update.min_candidates <= 100:
            raise HTTPException(status_code=400, detail="Minimum candidate pool must be between 1 and 100")
        if not 1 <= update.refresh_interval_minutes <= 60:
            raise HTTPException(status_code=400, detail="Refresh interval must be between 1 and 60 minutes")
        store.write_values(
            {
                "OPERATOR_PAPER_CAPITAL": str(update.paper_capital),
                "OPERATOR_TARGET_PCT": str(update.target_pct),
                "OPERATOR_STOP_PCT": str(update.stop_pct),
                "OPERATOR_MAX_CANDIDATES": str(update.max_candidates),
                "OPERATOR_TRADE_SLOTS": str(update.trade_slots),
                "OPERATOR_MIN_CANDIDATES": str(update.min_candidates),
                "OPERATOR_REFRESH_INTERVAL_MINUTES": str(update.refresh_interval_minutes),
                "OPERATOR_UNIVERSE_REFRESH_INTERVAL_MINUTES": str(update.universe_refresh_interval_minutes),
                "OPERATOR_MONITOR_INTERVAL_MINUTES": str(update.monitor_interval_minutes),
            }
        )
        # Re-register interval job so scheduler timing follows the newly saved setting.
        scheduler_service.stop()
        scheduler_service.start()
        return _operator_settings_payload(load_settings(env_path))

    @app.get("/api/operator/top-bets")
    def operator_top_bets():
        try:
            settings = load_settings(env_path)
            repo = TradingRepository(create_supabase_client(settings))
            rows = _operator_candidate_rows(settings, repo, env_path)
            return build_top_bets(rows, settings, open_rows=repo.list_entered_watchlist())
        except Exception as exc:
            _raise_api_error(exc, "operator_top_bets")

    @app.get("/api/operator/open-positions")
    def operator_open_positions():
        try:
            settings = load_settings(env_path)
            client = create_supabase_client(settings)
            repo = TradingRepository(client)
            engine = PaperExecutionEngine(repo, StrategyConfigRepository(client), AlpacaBroker(settings))
            positions = [
                asdict(status)
                for status in engine.open_position_statuses(
                    create_market_data_provider(settings, env_path=env_path),
                    max_hold_days=settings.pead_max_hold_days,
                )
            ]
            deployed = round(sum(float(row.get("position_value") or 0) for row in positions), 2)
            unrealized = round(sum(float(row.get("unrealized_pnl") or 0) for row in positions), 2)
            return {
                "count": len(positions),
                "deployed_capital": deployed,
                "available_capital": round(max(0.0, settings.operator_paper_capital - deployed), 2),
                "unrealized_pnl": unrealized,
                "exit_ready": sum(1 for row in positions if row.get("exit_reason")),
                "positions": positions,
            }
        except Exception as exc:
            _raise_api_error(exc, "operator_open_positions")

    @app.get("/api/operator/performance")
    def operator_performance():
        try:
            settings = load_settings(env_path)
            client = create_supabase_client(settings)
            repo = TradingRepository(client)
            engine = PaperExecutionEngine(repo, StrategyConfigRepository(client), AlpacaBroker(settings))
            positions = [
                asdict(status)
                for status in engine.open_position_statuses(
                    create_market_data_provider(settings, env_path=env_path),
                    max_hold_days=settings.pead_max_hold_days,
                )
            ]
            return _performance_payload(repo.list_recent_trades(limit=500), positions, settings.operator_paper_capital)
        except Exception as exc:
            _raise_api_error(exc, "operator_performance")

    @app.post("/api/operator/reset-paper-state")
    def reset_operator_paper_state(payload: ResetPaperStateRequest | None = None):
        payload = payload or ResetPaperStateRequest()
        try:
            scheduler_service.stop()
            settings = load_settings(env_path)
            client = create_supabase_client(settings)
            broker_result: Any = {"skipped": True}
            if payload.reset_broker:
                broker_result = AlpacaBroker(settings).reset_paper_account()
            reset_result = TradingRepository(client).reset_operator_paper_state()
            scheduler_service.state.last_runs.clear()
            scheduler_service.state.operator_universe_cursor = 0
            scheduler_service.start()
            return {
                "status": "reset",
                "database": reset_result,
                "broker": broker_result,
                "paper_capital": settings.operator_paper_capital,
                "scheduler_running": scheduler_service.status()["running"],
            }
        except Exception as exc:
            scheduler_service.start()
            _raise_api_error(exc, "operator_reset_paper_state")

    @app.post("/api/operator/qwen-review")
    def operator_qwen_review():
        try:
            settings = load_settings(env_path)
            repo = TradingRepository(create_supabase_client(settings))
            rows = build_top_bets(_operator_candidate_rows(settings, repo, env_path), settings, open_rows=repo.list_entered_watchlist())["candidates"]
            notes = {
                "instruction": "Review the operator candidate pool. Do not recommend bypassing risk rules. Summarize concentration, obvious duplicates, and entry/exit readiness.",
                "operator_settings": _operator_settings_payload(settings),
                "candidates": rows[: settings.operator_max_candidates],
            }
            review = active_adapter(str(env_path)).generate_evening_review(
                EveningInput(date=date.today(), trades=[], daily_pnl=0, daily_pnl_pct=0, notes=str(notes))
            )
            return review.model_dump(mode="json")
        except Exception as exc:
            _raise_api_error(exc, "operator_qwen_review")

    @app.post("/api/operator/approve-paper-trades")
    def operator_approve_paper_trades(payload: ApprovePaperTradesRequest):
        try:
            settings = load_settings(env_path)
            client = create_supabase_client(settings)
            trading_repo = TradingRepository(client)
            config_repo = StrategyConfigRepository(client)
            if not settings.paper_mode:
                raise HTTPException(status_code=400, detail="Live trading is blocked; PAPER_MODE must stay true")
            if not config_repo.is_trading_active():
                return {"submit": payload.submit, "requested": len(payload.selected_ids), "attempted": 0, "submitted": [], "skipped": [], "blocked_reason": "kill switch inactive"}
            rows = trading_repo.list_watchlist_by_ids([item for item in payload.selected_ids if not item.startswith("momentum:")])
            momentum_ids = {item.removeprefix("momentum:") for item in payload.selected_ids if item.startswith("momentum:")}
            if momentum_ids:
                market_data = create_market_data_provider(settings, env_path=env_path)
                momentum_rows = [row for row in trading_repo.list_recent_momentum_scores(limit=settings.operator_max_candidates) if str(row.get("ticker", "")).upper() in momentum_ids]
                prices = {}
                for item in momentum_rows:
                    ticker = str(item.get("ticker", "")).upper()
                    try:
                        prices[ticker] = _operator_price(market_data, ticker)
                    except Exception:
                        continue
                rows.extend(momentum_rows_to_operator_rows(momentum_rows, prices))
            return approve_paper_trades(
                rows=rows,
                selected_ids=payload.selected_ids,
                settings=settings,
                repository=trading_repo,
                broker=AlpacaBroker(settings),
                submit=payload.submit,
                open_rows=trading_repo.list_entered_watchlist(),
            )
        except HTTPException:
            raise
        except Exception as exc:
            _raise_api_error(exc, "operator_approve_paper_trades")

    @app.get("/api/universe/pead")
    def pead_universe():
        settings = load_settings(env_path)
        tickers = load_pead_universe(settings, env_path=env_path)
        source = "PEAD_SCAN_TICKERS" if settings.pead_scan_tickers else settings.pead_universe_file
        return {"source": source, "count": len(tickers), "tickers": tickers}

    @app.get("/api/earnings-events")
    def earnings_events():
        settings = load_settings(env_path)
        store = EarningsEventStore.from_env_path(settings.earnings_events_file, env_path=env_path)
        rows = []
        for ticker in load_pead_universe(settings, env_path=env_path):
            for event in store.events_for_ticker(ticker):
                rows.append(
                    {
                        "ticker": event.ticker,
                        "earnings_date": event.earnings_date.isoformat(),
                        "actual_eps": event.actual_eps,
                        "estimate_eps": event.estimate_eps,
                    }
                )
        rows.sort(key=lambda row: (row["earnings_date"], row["ticker"]), reverse=True)
        return {"source": settings.earnings_events_file, "count": len(rows), "events": rows[:100]}

    @app.post("/api/earnings-events/import")
    def import_earnings_events():
        settings = load_settings(env_path)
        tickers = load_pead_universe(settings, env_path=env_path)
        store = EarningsEventStore.from_env_path(settings.earnings_events_file, env_path=env_path)
        provider = create_market_data_provider(settings, env_path=env_path)
        events = []
        errors = {}
        for ticker in tickers:
            try:
                event = provider.latest_earnings_event(ticker, date.today())
                if event is not None:
                    events.append(event)
            except Exception as exc:
                errors[ticker] = str(exc)
        store.write_events(events)
        return {"source": "finnhub", "imported": len(events), "tickers": len(tickers), "errors": errors}

    @app.post("/api/scan-pead")
    def scan_pead(payload: PEADScanRequest):
        try:
            settings = load_settings(env_path)
            repository = TradingRepository(create_supabase_client(settings)) if payload.persist else None
            scorer = FinBERTSentimentScorer() if payload.sentiment == "finbert" else KeywordSentimentScorer()
            scanner = PEADScanner(
                create_market_data_provider(settings, env_path=env_path),
                scorer,
                repository,
                portfolio_value=settings.paper_portfolio_value,
                risk_pct=settings.risk_per_trade_pct,
                max_position_pct=settings.max_position_pct,
                target_pct=settings.pead_target_pct,
                stop_pct=settings.pead_stop_pct,
            )
            tickers = [ticker.strip().upper() for ticker in payload.tickers.split(",") if ticker.strip()]
            if not tickers:
                tickers = load_pead_universe(settings, env_path=env_path)
            if not tickers:
                raise HTTPException(status_code=400, detail="No tickers supplied and no PEAD universe configured")
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
                    "entry_price": getattr(result, "entry_price", None),
                    "target_price": getattr(result, "target_price", None),
                    "stop_loss": getattr(result, "stop_loss", None),
                    "shares": getattr(result, "shares", None),
                }
                for scan_date, result in results
            ]
        except Exception as exc:
            _raise_api_error(exc, "scan_pead")

    @app.post("/api/execute-pending")
    def execute_pending(payload: ExecutePendingRequest):
        try:
            settings = load_settings(env_path)
            client = create_supabase_client(settings)
            trading_repo = TradingRepository(client)
            config_repo = StrategyConfigRepository(client)
            vix = FredMacroDataProvider(settings).current_vix()
            pause = evaluate_daily_pause(
                trading_active=settings.trading_active and config_repo.is_trading_active(),
                vix=vix,
                daily_pnl_pct=_latest_daily_pnl_pct(trading_repo),
                spy_premarket_change_pct=create_market_data_provider(settings, env_path=env_path).spy_premarket_change_pct(),
                vix_threshold=settings.vix_pause_threshold,
                daily_loss_limit_pct=settings.daily_loss_limit_pct,
                spy_premarket_pause_pct=settings.spy_premarket_pause_pct,
            )
            if pause.paused:
                trading_repo.upsert_daily_summary(
                    {
                        "date": date.today().isoformat(),
                        "vix": vix,
                        "trading_active": False,
                        "pause_reason": pause.reason,
                    }
                )
                return {"attempted": 0, "submitted": 0, "blocked_reason": pause.reason, "skipped": 0, "vix": vix}
            engine = PaperExecutionEngine(
                trading_repo,
                config_repo,
                AlpacaBroker(settings),
            )
            summary = engine.execute_pending_watchlist(
                dry_run=not payload.submit,
                max_total_positions=settings.max_total_positions,
                max_pead_long_positions=settings.max_pead_long_positions,
                max_pead_short_positions=settings.max_pead_short_positions,
                max_momentum_positions=settings.max_momentum_positions,
            )
            result = _object_dict(summary)
            result["vix"] = vix
            return result
        except Exception as exc:
            _raise_api_error(exc, "execute_pending")

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
            if job_id == "position_management":
                return scheduler_service.run_position_management()
            if job_id == "weekly_momentum_scan":
                return scheduler_service.run_momentum_scan()
            if job_id == "operator_candidate_refresh":
                return scheduler_service.run_operator_candidate_refresh()
            if job_id == "operator_universe_refresh":
                return scheduler_service.run_operator_universe_refresh()
            if job_id == "realtime_entry_monitor":
                return scheduler_service.run_realtime_entry_monitor()
            if job_id == "realtime_exit_monitor":
                return scheduler_service.run_realtime_exit_monitor()
            raise HTTPException(status_code=404, detail=f"Unknown scheduler job: {job_id}")
        except HTTPException:
            raise
        except Exception as exc:
            _raise_api_error(exc, f"scheduler_run:{job_id}")

    @app.post("/api/kill-switch")
    def set_kill_switch(active: bool):
        try:
            repo = StrategyConfigRepository(create_supabase_client(load_settings(env_path)))
            repo.set_trading_active(active)
            return {"trading_active": active}
        except Exception as exc:
            _raise_api_error(exc, "kill_switch")

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


def _latest_daily_pnl_pct(repo: TradingRepository) -> float:
    rows = repo.list_daily_summaries(limit=1)
    if not rows:
        return 0.0
    value = rows[0].get("pnl_pct")
    return float(value) if value is not None else 0.0


def _operator_price(market_data, ticker: str) -> float:
    try:
        price = market_data.company_profile(ticker).current_price
        if price > 0:
            return price
    except Exception:
        pass
    history = market_data.daily_history(ticker, period="10d")
    return float(history["close"].dropna().iloc[-1])


def _operator_candidate_rows(settings, repo: TradingRepository, env_path: Path | str) -> list[dict]:
    rows = repo.list_candidate_watchlist()
    if rows:
        return rows
    active_tickers = {
        str(row.get("ticker", "")).upper()
        for row in repo.list_entered_watchlist()
        if row.get("ticker")
    }
    market_data = create_market_data_provider(settings, env_path=env_path)
    momentum_rows = [
        row
        for row in repo.list_recent_momentum_scores(limit=max(settings.operator_max_candidates * 3, 10))
        if str(row.get("ticker", "")).upper() not in active_tickers
    ][: settings.operator_max_candidates]
    prices = {}
    for item in momentum_rows:
        ticker = str(item.get("ticker", "")).upper()
        if not ticker:
            continue
        try:
            prices[ticker] = _operator_price(market_data, ticker)
        except Exception:
            continue
    return momentum_rows_to_operator_rows(momentum_rows, prices)


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    for key in (
        "SUPABASE_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "FRED_API_KEY",
        "FINNHUB_API_KEY",
        "FMP_API_KEY",
        "POLYGON_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "QWEN_API_KEY",
    ):
        value = os.getenv(key, "")
        if value:
            text = text.replace(value, "<redacted>")
    return text


def _operator_settings_payload(settings) -> dict:
    return {
        "paper_capital": settings.operator_paper_capital,
        "target_pct": settings.operator_target_pct,
        "stop_pct": settings.operator_stop_pct,
        "max_candidates": settings.operator_max_candidates,
        "trade_slots": settings.operator_trade_slots,
        "min_candidates": settings.operator_min_candidates,
        "refresh_interval_minutes": settings.operator_refresh_interval_minutes,
        "universe_refresh_interval_minutes": settings.operator_universe_refresh_interval_minutes,
        "monitor_interval_minutes": settings.operator_monitor_interval_minutes,
    }


def _performance_payload(trades: list[dict], positions: list[dict], paper_capital: float) -> dict:
    closed = [_trade_with_pnl(row) for row in trades]
    priced = [row for row in closed if row["pnl"] is not None]
    realized_profit = round(sum(row["pnl"] for row in priced if row["pnl"] > 0), 2)
    realized_loss = round(abs(sum(row["pnl"] for row in priced if row["pnl"] < 0)), 2)
    realized_pnl = round(realized_profit - realized_loss, 2)
    unrealized_pnl = round(sum(float(row.get("unrealized_pnl") or 0) for row in positions), 2)
    wins = sum(1 for row in priced if row["pnl"] > 0)
    losses = sum(1 for row in priced if row["pnl"] < 0)
    breakeven = sum(1 for row in priced if row["pnl"] == 0)
    trade_count = len(priced)
    total_pnl = round(realized_pnl + unrealized_pnl, 2)
    return {
        "paper_capital": paper_capital,
        "realized_profit": realized_profit,
        "realized_loss": realized_loss,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "total_pnl_pct": round(total_pnl / paper_capital, 4) if paper_capital else None,
        "win_count": wins,
        "loss_count": losses,
        "breakeven_count": breakeven,
        "closed_trade_count": trade_count,
        "unpriced_exit_count": len(closed) - trade_count,
        "win_rate": round(wins / trade_count * 100, 2) if trade_count else 0.0,
        "recent_exits": closed[:20],
    }


def _trade_with_pnl(row: dict) -> dict:
    pnl = _number(row.get("pnl"))
    entry = _number(row.get("entry_price"))
    exit_price = _number(row.get("exit_price"))
    shares = int(row.get("shares") or 0)
    side = str(row.get("side") or "")
    if pnl is None and entry is not None and exit_price is not None and shares:
        pnl = (entry - exit_price) * shares if side in {"short", "sell_short", "cover"} else (exit_price - entry) * shares
    position_value = abs(entry * shares) if entry is not None else None
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "ticker": row.get("ticker"),
        "strategy": row.get("strategy"),
        "side": side,
        "shares": shares,
        "entry_price": entry,
        "exit_price": exit_price,
        "pnl": round(pnl, 2) if pnl is not None else None,
        "pnl_pct": _number(row.get("pnl_pct")) if row.get("pnl_pct") is not None else (round(pnl / position_value, 4) if pnl is not None and position_value else None),
        "exit_reason": row.get("exit_reason"),
        "hold_days": row.get("hold_days"),
    }


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _raise_api_error(exc: Exception, context: str, *, status_code: int = 503) -> NoReturn:
    logger.exception("api_error", extra={"context": context, "error": _safe_error(exc)})
    raise HTTPException(status_code=status_code, detail={"message": _safe_error(exc), "context": context}) from exc


app = create_app()
