from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import NoReturn

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from driftpilot.dashboard.agent_views import agent_dashboard_payload, agent_decision_detail
from driftpilot.dashboard.view_models import admin_state_payload, backtest_report_payload, diagnostics_payload, operator_state_payload
from driftpilot.settings import load_settings as load_driftpilot_settings
from driftpilot.storage.repositories import DriftPilotRepository


TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
logger = logging.getLogger("trading_bot.dashboard")


def create_app(env_path: Path | str = ".env") -> FastAPI:
    app = FastAPI(title="AI Trading Bot Dashboard")

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
        return {"ok": True, "service": "driftpilot-dashboard"}

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

    # ── Brain Proxy Endpoints (forward to DGX brain server) ──

    @app.get("/api/brain/stats")
    def brain_stats():
        """Brain health stats — proxied from DGX brain server."""
        try:
            from driftpilot.agents.brain_client import BrainClient
            client = BrainClient()
            stats = client.get_stats()
            if stats is None:
                return {"status": "unavailable", "message": "Brain server not reachable"}
            return {"status": "ok", **stats}
        except Exception as exc:
            _raise_api_error(exc, "brain_stats")

    @app.get("/api/brain/skills")
    def brain_skills(status: str = "active"):
        """Active brain skills."""
        try:
            from driftpilot.agents.brain_client import BrainClient
            client = BrainClient()
            skills = client.get_skills(status=status)
            return {"status": "ok", "skills": skills, "count": len(skills)}
        except Exception as exc:
            _raise_api_error(exc, "brain_skills")

    @app.get("/api/operator/pm-analysis")
    def pm_analysis():
        """Latest PM Analyst analysis — structured trade health report."""
        try:
            from driftpilot.agents.pm_analyst import PMAnalyst
            dp_settings = load_driftpilot_settings(env_path)
            analyst = PMAnalyst(
                operator_db_path=dp_settings.sqlite_path,
                qwen_url=dp_settings.agent_qwen_url,
                qwen_model=dp_settings.agent_qwen_model,
            )
            latest = analyst.get_latest()
            if latest is None:
                return {"status": "no_analysis", "message": "No PM analysis yet. Will run on next 15-min cycle."}
            return {"status": "ok", "analysis": latest}
        except Exception as exc:
            _raise_api_error(exc, "pm_analysis")

    @app.post("/api/operator/pm-analysis/run")
    def pm_analysis_run():
        """Trigger an immediate PM analysis (for testing or manual refresh)."""
        try:
            from driftpilot.agents.pm_analyst import PMAnalyst
            dp_settings = load_driftpilot_settings(env_path)
            analyst = PMAnalyst(
                operator_db_path=dp_settings.sqlite_path,
                qwen_url=dp_settings.agent_qwen_url,
                qwen_model=dp_settings.agent_qwen_model,
            )
            result = analyst.run()
            if result is None:
                return {"status": "skipped", "message": "No trades or positions to analyze."}
            return {"status": "ok", "analysis": result}
        except Exception as exc:
            _raise_api_error(exc, "pm_analysis_run")

    @app.get("/api/operator/pm-analysis/history")
    def pm_analysis_history(limit: int = 10):
        """PM analysis history for trend tracking."""
        try:
            from driftpilot.agents.pm_analyst import PMAnalyst
            dp_settings = load_driftpilot_settings(env_path)
            analyst = PMAnalyst(
                operator_db_path=dp_settings.sqlite_path,
                qwen_url=dp_settings.agent_qwen_url,
                qwen_model=dp_settings.agent_qwen_model,
            )
            return {"status": "ok", "history": analyst.get_history(limit=limit)}
        except Exception as exc:
            _raise_api_error(exc, "pm_analysis_history")

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

    return app


def _object_dict(value) -> dict:
    if hasattr(value, "__dict__") and value.__dict__:
        return dict(value.__dict__)
    return {
        key: getattr(value, key)
        for key in dir(value)
        if not key.startswith("_") and not callable(getattr(value, key))
    }


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


def _raise_api_error(exc: Exception, context: str, *, status_code: int = 503) -> NoReturn:
    logger.exception("api_error", extra={"context": context, "error": _safe_error(exc)})
    raise HTTPException(status_code=status_code, detail={"message": _safe_error(exc), "context": context}) from exc


app = create_app()
