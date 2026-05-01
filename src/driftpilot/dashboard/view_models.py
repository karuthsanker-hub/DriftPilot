from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from driftpilot.settings import DriftPilotSettings
from driftpilot.storage.repositories import DriftPilotRepository


def operator_state_payload(settings: DriftPilotSettings) -> dict[str, Any]:
    db_path = settings.sqlite_path_obj
    if db_path.exists():
        try:
            repo = DriftPilotRepository.open(db_path)
            return _payload_from_repo(repo, settings)
        except Exception as exc:
            return _mock_payload(settings) | {
                "state": "ERROR",
                "halt_banner": f"State database read failed: {exc}",
                "source": "mock_after_error",
            }
    return _mock_payload(settings)


def backtest_report_payload(path: str | Path = "expectancy_report.json") -> dict[str, Any]:
    report_path = Path(path)
    if report_path.exists():
        loaded = json.loads(report_path.read_text())
        if not isinstance(loaded, dict):
            raise ValueError("expectancy_report.json must contain an object")
        loaded.setdefault("source", "file")
        return loaded
    return _mock_backtest_report()


def admin_state_payload(settings: DriftPilotSettings) -> dict[str, Any]:
    db_path = settings.sqlite_path_obj
    sqlite_exists = db_path.exists()
    state = None
    latest = None
    if sqlite_exists:
        try:
            repo = DriftPilotRepository.open(db_path)
            state = repo.state.get()
            latest = repo.transitions.latest()
        except Exception as exc:
            return {
                "system_health": {"state_db": "ERROR", "message": str(exc)},
                "manual_override": _manual_override_payload(),
                "broker_reconciliation": {"status": "unknown", "mismatches": []},
                "event_log": [],
                "configuration": _safe_config(settings),
            }
    return {
        "system_health": {
            "state_db": "OK" if sqlite_exists else "MISSING",
            "operator_state": state.current_state if state else "BOOT",
            "mode": settings.mode.upper(),
            "sip_feed": settings.alpaca_data_feed,
        },
        "manual_override": _manual_override_payload(),
        "broker_reconciliation": {
            "status": "matched" if latest else "not_run",
            "last_reason": latest.reason if latest else "No reconciliation event yet",
            "mismatches": [],
        },
        "event_log": [
            {
                "time": latest.timestamp.isoformat(),
                "state": latest.to_state,
                "reason": latest.reason,
            }
        ] if latest else [],
        "configuration": _safe_config(settings),
    }


def _payload_from_repo(repo: DriftPilotRepository, settings: DriftPilotSettings) -> dict[str, Any]:
    current = repo.state.get()
    slots = repo.slots.list_all()
    latest = repo.transitions.latest()
    payload = _mock_payload(settings)
    payload["source"] = "sqlite"
    payload["state"] = current.current_state if current else "BOOT"
    payload["halt_banner"] = latest.reason if latest else "Waiting for first operator transition"
    payload["slots"] = [
        {
            "id": slot.slot_id,
            "state": slot.status,
            "symbol": slot.symbol,
            "entry": None,
            "current": None,
            "pnl_pct": None,
            "time_min": None,
            "sector": (slot.metadata or {}).get("sector"),
            "slippage": None,
            "empty_reason": "Awaiting candidate" if slot.status.upper() == "EMPTY" else None,
        }
        for slot in slots
    ] or payload["slots"]
    payload["event_log"] = [
        {
            "time": latest.timestamp.isoformat(),
            "state": latest.to_state,
            "reason": latest.reason,
        }
    ] if latest else []
    return payload


def _mock_payload(settings: DriftPilotSettings) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "source": "mock",
        "mode": settings.mode.upper(),
        "state": "SCANNING",
        "regime": {
            "label": "CAUTION",
            "detail": "SPY below VWAP; entries require relative strength > 0.5%",
            "spy_5m_return": -0.0018,
            "spy_vwap_distance_pct": -0.0007,
        },
        "heartbeat": {"label": "SIP feed", "age_seconds": 0.4, "stale": False},
        "session": {"time": now, "market_clock": "regular_session", "cycle_seconds": settings.scan_interval_seconds},
        "equity": {
            "value": settings.paper_capital + 247.83,
            "floor": settings.equity_floor,
            "daily_pnl": 247.83,
            "daily_pnl_pct": 0.0248,
            "daily_trade_count": 14,
            "win_rate": 0.643,
        },
        "halt_banner": "CAUTION regime: entries restricted to symbols with relative strength > 0.5%",
        "slots": [
            {"id": 1, "state": "OPEN", "symbol": "NVDA", "entry": 487.32, "current": 491.15, "pnl_pct": 0.0079, "time_min": 12, "sector": "Tech", "slippage": 0.04},
            {"id": 2, "state": "OPEN", "symbol": "AVGO", "entry": 1342.50, "current": 1358.20, "pnl_pct": 0.0117, "time_min": 8, "sector": "Tech", "slippage": 0.05},
            {"id": 3, "state": "EXITING", "symbol": "CRWD", "entry": 287.40, "current": 290.27, "pnl_pct": 0.0100, "time_min": 23, "sector": "Tech", "slippage": 0.03, "exit_reason": "TARGET"},
            {"id": 4, "state": "OPEN", "symbol": "LLY", "entry": 612.18, "current": 609.44, "pnl_pct": -0.0045, "time_min": 17, "sector": "Health", "slippage": 0.06},
            {"id": 5, "state": "OPEN", "symbol": "PANW", "entry": 318.92, "current": 321.60, "pnl_pct": 0.0084, "time_min": 6, "sector": "Tech", "slippage": 0.03},
            {"id": 6, "state": "OPEN", "symbol": "XOM", "entry": 113.27, "current": 113.15, "pnl_pct": -0.0011, "time_min": 31, "sector": "Energy", "slippage": 0.02},
            {"id": 7, "state": "RESERVED", "symbol": "AMD", "sector": "Tech"},
            {"id": 8, "state": "EMPTY", "empty_reason": "Sector cap: TECH 3/3"},
            {"id": 9, "state": "OPEN", "symbol": "COST", "entry": 891.40, "current": 894.22, "pnl_pct": 0.0032, "time_min": 4, "sector": "Cons.Stap", "slippage": 0.05},
            {"id": 10, "state": "EMPTY", "empty_reason": "Awaiting candidate"},
        ],
        "candidate_queue": [
            {"rank": 1, "symbol": "AMD", "score": 2.84, "rvol": 3.2, "return_15m": 0.0092, "vwap_distance_pct": 0.014, "sector": "Tech", "status": "RES"},
            {"rank": 2, "symbol": "MU", "score": 2.61, "rvol": 4.1, "return_15m": 0.0078, "vwap_distance_pct": 0.011, "sector": "Tech", "status": "CAP"},
            {"rank": 3, "symbol": "SMCI", "score": 2.43, "rvol": 2.8, "return_15m": 0.0134, "vwap_distance_pct": 0.021, "sector": "Tech", "status": "CAP"},
            {"rank": 4, "symbol": "UNH", "score": 2.21, "rvol": 2.4, "return_15m": 0.0061, "vwap_distance_pct": 0.009, "sector": "Health", "status": "Q"},
            {"rank": 5, "symbol": "CVX", "score": 2.07, "rvol": 2.1, "return_15m": 0.0055, "vwap_distance_pct": 0.008, "sector": "Energy", "status": "Q"},
        ],
        "recycle_log": [
            {"time": "10:34:18", "slot": 3, "from": "CRWD", "exit": "TARGET", "pnl_pct": 0.0100, "to": None},
            {"time": "10:28:42", "slot": 7, "from": "TSLA", "exit": "STOP", "pnl_pct": -0.0102, "to": "AMD"},
            {"time": "10:21:07", "slot": 2, "from": "META", "exit": "TARGET", "pnl_pct": 0.0104, "to": "AVGO"},
        ],
        "event_log": [],
        "equity_curve": [{"t": index, "equity": settings.paper_capital + (index * 4.2)} for index in range(24)],
    }


def _mock_backtest_report() -> dict[str, Any]:
    return {
        "source": "mock",
        "schema_version": 1,
        "period": {"start": "2024-01-01", "end": "2024-12-31"},
        "verdict": "GATED",
        "live_gate": {
            "backtest_expectancy_positive": True,
            "paper_trading_60_days_positive_pnl_sharpe_gt_1": False,
            "equity_floor_buffer": False,
            "live_ok_env": False,
        },
        "metrics": {
            "total_return_pct": 0.1142,
            "gross_return_pct": 0.1871,
            "slippage_return_pct": 0.0729,
            "total_pnl": 1142.0,
            "gross_pnl": 1871.0,
            "slippage_cost": 729.0,
            "total_trades": 1847,
            "win_rate": 0.537,
            "average_hold_minutes": 18.4,
            "expectancy_per_trade": 0.62,
            "expectancy_per_dollar": 0.00062,
            "sharpe": 1.34,
            "max_drawdown_pct": -0.0618,
            "exit_breakdown": {"TARGET": 673, "STOP": 641, "TIME": 533},
            "regime_performance": {
                "GREEN": {"trades": 1243, "win_rate": 0.562, "expectancy_per_trade": 0.78, "pnl": 970.0},
                "CAUTION": {"trades": 472, "win_rate": 0.518, "expectancy_per_trade": 0.41, "pnl": 194.0},
                "RED": {"trades": 132, "win_rate": 0.477, "expectancy_per_trade": -0.17, "pnl": -22.0},
            },
            "daily_pnl": {f"2024-01-{day:02d}": (day - 10) * 7.5 for day in range(1, 21)},
        },
        "slippage_waterfall": {
            "gross_return_pct": 0.1871,
            "slippage_cost_pct": -0.0729,
            "net_return_pct": 0.1142,
        },
        "constituents": {
            "point_in_time": False,
            "survivorship_bias_note": "Point-in-time constituents were unavailable; results may include survivorship bias.",
        },
        "caveats": [
            "Point-in-time constituents unavailable in this mock report.",
            "Slippage is modeled, not measured from live fills.",
            "Outage simulation is not included in Phase 7b.",
        ],
        "equity_curve": [
            {"timestamp": f"2024-01-{day:02d}T16:00:00+00:00", "equity": 10000 + (day * 45)}
            for day in range(1, 21)
        ],
    }


def _manual_override_payload() -> dict[str, Any]:
    return {
        "pause_scanning_enabled": True,
        "flat_all_positions_enabled": True,
        "requires_confirmation": True,
        "note": "Manual overrides are emergency-only and write state-machine events.",
    }


def _safe_config(settings: DriftPilotSettings) -> dict[str, Any]:
    return {
        "mode": settings.mode,
        "paper_capital": settings.paper_capital,
        "trade_slots": settings.trade_slots,
        "slot_value": settings.slot_value,
        "target_pct": settings.target_pct,
        "stop_pct": settings.stop_pct,
        "max_hold_minutes": settings.max_hold_minutes,
        "scan_interval_seconds": settings.scan_interval_seconds,
        "max_trades_per_day": settings.max_trades_per_day,
        "daily_loss_limit_pct": settings.daily_loss_limit_pct,
        "equity_floor": settings.equity_floor,
        "alpaca_data_feed": settings.alpaca_data_feed,
    }
