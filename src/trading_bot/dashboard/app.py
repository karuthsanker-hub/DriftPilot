from __future__ import annotations

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

    @app.get("/api/operator/pipeline")
    def operator_pipeline():
        """Candidate pipeline: last N scan cycles with full decision audit.

        Shows every candidate considered, why it was accepted/rejected,
        dynamic band calculations, slot status, and open positions.
        """
        try:
            import json as _json
            import sqlite3
            from datetime import datetime, timezone
            from pathlib import Path as _Path

            # Pipeline scan cycles
            pipeline_path = _Path("data/driftpilot/pipeline_log.json")
            cycles = []
            if pipeline_path.exists():
                cycles = _json.loads(pipeline_path.read_text())

            # Slot + position status from operator DB
            slots = []
            positions = []
            open_symbols_for_quotes: set[str] = set()
            db_path = _Path("data/driftpilot/operator_state.sqlite3")
            if db_path.exists():
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    "SELECT slot_id, status, symbol, metadata_json FROM slots ORDER BY slot_id"
                ):
                    meta = _json.loads(row["metadata_json"] or "{}")
                    slots.append({
                        "slot_id": row["slot_id"],
                        "status": row["status"],
                        "symbol": row["symbol"],
                        "entry_price": meta.get("entry_price"),
                        "opened_at": meta.get("opened_at"),
                    })
                for row in conn.execute(
                    "SELECT symbol, quantity, entry_price, target_price, stop_price, status, "
                    "opened_at, metadata_json FROM positions WHERE status='open' "
                    "ORDER BY opened_at DESC"
                ):
                    meta = _json.loads(row["metadata_json"] or "{}")
                    current_price = _coerce_float(meta.get("current_price"))
                    open_symbols_for_quotes.add(row["symbol"])
                    positions.append({
                        "symbol": row["symbol"],
                        "quantity": row["quantity"],
                        "entry_price": row["entry_price"],
                        "target_price": row["target_price"],
                        "stop_price": row["stop_price"],
                        "opened_at": row["opened_at"],
                        "current_price": current_price,
                        "signal_name": meta.get("signal_name"),
                        "sector": meta.get("sector"),
                        "dynamic_target_pct": meta.get("dynamic_target_pct"),
                        "dynamic_stop_pct": meta.get("dynamic_stop_pct"),
                        "band_reasoning": meta.get("dynamic_band_reasoning"),
                        "band_atr_pct": meta.get("band_atr_pct"),
                        "band_beta": meta.get("band_beta"),
                        "band_drift_pct": meta.get("band_drift_pct"),
                        "band_rvol": meta.get("band_rvol"),
                        "catalyst_headline": meta.get("catalyst_headline"),
                        "catalyst_sentiment": meta.get("catalyst_sentiment"),
                    })
                conn.close()

            latest_prices = _latest_pipeline_prices(open_symbols_for_quotes, env_path)
            now_utc = datetime.now(timezone.utc)
            total_unrealized_pnl = 0.0
            for position in positions:
                symbol = position["symbol"]
                entry_price = _coerce_float(position.get("entry_price")) or 0.0
                quantity = _coerce_float(position.get("quantity")) or 0.0
                current_price = latest_prices.get(symbol) or position.get("current_price") or entry_price
                position["current_price"] = current_price
                unrealized_pnl = (current_price - entry_price) * quantity
                position["unrealized_pnl"] = unrealized_pnl
                position["unrealized_pct"] = (
                    (current_price / entry_price - 1.0) * 100.0
                    if entry_price > 0
                    else 0.0
                )
                position["time_held_minutes"] = _held_minutes(position.get("opened_at"), now_utc)
                total_unrealized_pnl += unrealized_pnl

            total_slots = len(slots) or 10
            free_slots = sum(1 for s in slots if s["status"] in ("available", "FREE"))
            open_symbols = {s["symbol"] for s in slots if s["symbol"]}

            # Mark which accepted candidates are queued vs would-enter
            for cycle in cycles:
                for cand in cycle.get("candidates", []):
                    if cand["symbol"] in open_symbols:
                        cand["queue_status"] = "already_open"
                    elif free_slots > 0:
                        cand["queue_status"] = "would_enter"
                    else:
                        cand["queue_status"] = "waiting"

            return {
                "cycles": cycles,
                "slots": slots,
                "positions": positions,
                "total_slots": total_slots,
                "free_slots": free_slots,
                "open_count": total_slots - free_slots,
                "total_unrealized_pnl": total_unrealized_pnl,
                "status": "ok",
                "count": len(cycles),
            }
        except Exception as exc:
            _raise_api_error(exc, "operator_pipeline")

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline_page(request: Request):
        return templates.TemplateResponse(request, "pipeline.html")

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

    @app.get("/api/brain/status")
    def brain_status():
        """Combined brain dashboard payload with graceful offline behavior."""
        try:
            from driftpilot.agents.brain_client import BrainClient

            client = BrainClient(timeout=2.0)
            stats = client.get_stats()
            if stats is None:
                return {
                    "status": "unavailable",
                    "message": "Brain server not reachable",
                    "stats": {},
                    "skills": [],
                    "experiences": [],
                    "reflections": [],
                }
            skills = client.get_skills(status="active")
            experiences = _brain_get_list(
                client,
                "/brain/experiences/recent",
                "experiences",
                {"limit": 20},
            )
            reflections = _brain_get_list(
                client,
                "/brain/reflections",
                "reflections",
                {"limit": 20},
            )
            last_reflection = reflections[0].get("date") if reflections else None
            return {
                "status": "ok",
                "stats": {**stats, "last_reflection_date": last_reflection},
                "skills": skills,
                "experiences": experiences,
                "reflections": reflections,
            }
        except Exception as exc:
            _raise_api_error(exc, "brain_status")

    @app.get("/brain", response_class=HTMLResponse)
    def brain_page(request: Request):
        return templates.TemplateResponse(request, "brain.html")

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


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _held_minutes(opened_at: Any, now_utc) -> float | None:
    if not opened_at:
        return None
    try:
        opened = opened_at if hasattr(opened_at, "tzinfo") else None
        if opened is None:
            from datetime import datetime, timezone

            opened = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
        return max(0.0, round((now_utc - opened.astimezone(now_utc.tzinfo)).total_seconds() / 60.0, 1))
    except (TypeError, ValueError) as exc:
        logger.debug("position opened_at parse failed: %s", exc)
        return None


def _latest_pipeline_prices(symbols: set[str], env_path: Path | str) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        from driftpilot.market_data.rest_quotes import AlpacaRestQuoteProvider

        settings = load_driftpilot_settings(env_path)
        if not settings.alpaca_key_id or not settings.alpaca_secret_key:
            return {}
        quote_provider = AlpacaRestQuoteProvider(
            settings.alpaca_key_id,
            settings.alpaca_secret_key,
            cache_ttl_s=10.0,
        )
        prices: dict[str, float] = {}
        for symbol in sorted(symbols):
            quote = quote_provider.latest_quote(symbol)
            if quote is None:
                continue
            prices[symbol] = (quote.bid_price + quote.ask_price) / 2.0
        return prices
    except Exception as exc:
        logger.warning("pipeline quote refresh failed: %s", exc)
        return {}


def _brain_get_list(client: Any, path: str, key: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        response = client._client.get(path, params=params)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        data = response.json()
        items = data.get(key, [])
        return items if isinstance(items, list) else []
    except Exception as exc:
        logger.warning("brain list fetch failed for %s: %s", path, exc)
        return []


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
