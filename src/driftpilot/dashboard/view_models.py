from __future__ import annotations

from datetime import UTC, datetime
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
