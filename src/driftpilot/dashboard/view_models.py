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
            transitions = repo.transitions.list_latest(limit=50)
        except Exception as exc:
            return {
                "system_health": {"state_db": "ERROR", "message": str(exc)},
                "manual_override": _manual_override_payload(),
                "broker_reconciliation": {"status": "unknown", "mismatches": []},
                "event_log": [],
                "configuration": _safe_config(settings),
            }
    else:
        transitions = []
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
                "time": transition.timestamp.isoformat(),
                "state": transition.to_state,
                "reason": transition.reason,
                "metadata": transition.metadata,
            }
            for transition in transitions
        ],
        "configuration": _safe_config(settings),
    }


def _payload_from_repo(repo: DriftPilotRepository, settings: DriftPilotSettings) -> dict[str, Any]:
    current = repo.state.get()
    slots = repo.slots.list_all()
    latest = repo.transitions.latest()
    positions = {position.id: position for position in repo.positions.list_open()}
    candidates = repo.list_candidates(limit=20)
    recycle_events = repo.list_recycle_events(limit=20)
    transitions = repo.transitions.list_latest(limit=20)
    regime_label = _latest_regime_label(current.metadata if current else None, transitions)
    report = backtest_report_payload()
    backtest_failed = report.get("verdict") == "FAIL"
    payload = _mock_payload(settings)
    payload["source"] = "sqlite"
    payload["state"] = current.current_state if current else "BOOT"
    payload["halt_banner"] = _halt_banner(current.current_state if current else "BOOT", latest.reason if latest else None, backtest_failed)
    payload["regime"] = {
        "label": regime_label,
        "detail": "Paper trading allowed; live trading remains gated" if backtest_failed else "Runtime state from SQLite",
    }
    payload["heartbeat"] = {"label": "synthetic feed", "age_seconds": 0, "stale": False}
    payload["session"] = {
        "time": (current.updated_at if current else datetime.now(UTC)).isoformat(),
        "market_clock": (current.metadata or {}).get("feed", "sqlite") if current else "sqlite",
        "cycle_seconds": settings.scan_interval_seconds,
    }
    realized_total = _realized_pnl(repo)
    realized_today = _realized_pnl_today(repo)
    today_trades = _trade_count_today(repo)
    deployed_local = sum(position.entry_price * position.quantity for position in positions.values())

    # Prefer live Alpaca account data when creds are configured. The local
    # paper_capital is a fallback estimate that doesn't reflect the real
    # account size and breaks "deployed > equity" displays at scale.
    live = _live_alpaca_equity(settings)
    if live is not None:
        equity_value = live["equity"]
        deployed = live["positions_mv"]  # broker-truth deployed (incl. drift)
        available = live["buying_power"]
        equity_source = "alpaca_live"
        # P&L percentages computed against actual account equity, not the
        # capped local paper_capital.
        pnl_baseline = max(equity_value, 1.0)
    else:
        equity_value = settings.paper_capital + realized_total
        deployed = deployed_local
        available = max(0, settings.paper_capital - deployed_local)
        equity_source = "local_simulated"
        pnl_baseline = settings.paper_capital if settings.paper_capital else 1.0

    payload["equity"] = {
        "value": equity_value,
        "source": equity_source,
        "floor": settings.equity_floor,
        # cumulative_pnl: realized lifetime across all closed trades
        "cumulative_pnl": realized_total,
        "cumulative_pnl_pct": realized_total / pnl_baseline,
        # today_pnl: realized just for today's session (UTC date)
        "today_pnl": realized_today,
        "today_pnl_pct": realized_today / pnl_baseline,
        # daily_pnl kept for back-compat but now means today's realized
        "daily_pnl": realized_today,
        "daily_pnl_pct": realized_today / pnl_baseline,
        "daily_trade_count": today_trades,
        "win_rate": _win_rate(repo),
        "win_rate_today": _win_rate_today(repo),
        "deployed": deployed,
        "available": available,
    }
    # New panel: last 20 closed trades with full chain (entry, exit, PnL,
    # hold, catalyst headline). Lets the UI show what got bought, what got
    # sold, and the result, in chronological order.
    payload["recent_trades"] = _recent_trades(repo, limit=20)
    # Scrolling news ticker — most recent catalyst events from the catalyst DB
    payload["news_ticker"] = _news_ticker(limit=30, lookback_minutes=240)
    # Build a symbol-keyed lookup for local positions (slots don't always carry
    # position_id) and pass live Alpaca per-symbol data so slot cards show
    # current price + unrealized %.
    positions_by_symbol_db = {
        (p.symbol or "").upper(): p
        for p in positions.values()
    }
    live_positions_map = (live or {}).get("positions_by_symbol", {}) if live else {}
    payload["slots"] = [
        _slot_payload(slot, positions, positions_by_symbol_db, live_positions_map)
        for slot in slots
    ] or payload["slots"]
    payload["candidate_queue"] = [
        {
            "rank": index,
            "symbol": candidate.symbol,
            "score": candidate.score,
            "rvol": candidate.rvol,
            "return_15m": candidate.return_15m_pct,
            "vwap_distance_pct": candidate.vwap_distance_pct,
            "sector": candidate.sector,
            "status": _candidate_status(candidate.queue_status, candidate.blocked_reason),
            "blocked_reason": candidate.blocked_reason,
        }
        for index, candidate in enumerate(candidates, start=1)
    ]
    payload["recycle_log"] = [
        {
            "time": event.at.astimezone(UTC).strftime("%H:%M:%S"),
            "slot": event.slot_id,
            "from": event.freed_symbol,
            "exit": event.exit_reason,
            "pnl_pct": event.exit_pnl_pct,
            "to": event.replacement_symbol,
        }
        for event in recycle_events
    ]
    payload["event_log"] = [
        {
            "time": transition.timestamp.isoformat(),
            "state": transition.to_state,
            "reason": transition.reason,
        }
        for transition in transitions
    ]
    payload["equity_curve"] = _equity_curve(settings.paper_capital, realized_total)
    return payload


def _latest_regime_label(current_metadata: dict[str, Any] | None, transitions: list[Any]) -> str:
    if current_metadata and current_metadata.get("regime"):
        return str(current_metadata["regime"])
    for transition in transitions:
        metadata = transition.metadata or {}
        if metadata.get("regime"):
            return str(metadata["regime"])
    return "UNKNOWN"


def _slot_payload(
    slot: Any,
    positions: dict[int, Any],
    positions_by_symbol_db: dict[str, Any] | None = None,
    live_positions: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    metadata = slot.metadata or {}
    sym = (slot.symbol or "").upper() if slot.symbol else None
    # Look up the local position by id first, then by symbol (slots in our
    # SQLite don't always carry position_id).
    position = positions.get(slot.position_id or -1)
    if position is None and sym and positions_by_symbol_db:
        position = positions_by_symbol_db.get(sym)

    # Live Alpaca data takes precedence for current_price + unrealized.
    live = (live_positions or {}).get(sym) if sym else None
    entry = (
        live["avg_entry"] if live else
        (position.entry_price if position is not None else metadata.get("entry_price"))
    )
    current = (
        live["mark"] if live else
        metadata.get("current_price", entry)
    )
    pnl_pct = None
    time_min = None
    if entry is not None:
        try:
            entry_value = float(entry)
            current_value = float(current if current is not None else entry)
            if entry_value > 0:
                pnl_pct = (current_value - entry_value) / entry_value
        except (TypeError, ValueError):
            pass
        if position is not None:
            try:
                time_min = int((datetime.now(UTC) - position.opened_at.astimezone(UTC)).total_seconds() // 60)
            except Exception:
                pass
    return {
        "id": slot.slot_id,
        "state": slot.status,
        "symbol": slot.symbol,
        "entry": entry,
        "current": current,
        "pnl_pct": pnl_pct,
        "time_min": time_min,
        "sector": metadata.get("sector"),
        "slippage": metadata.get("slippage"),
        "empty_reason": metadata.get("empty_reason") or ("Awaiting candidate" if slot.status.upper() == "EMPTY" else None),
    }


def _candidate_status(status: str, blocked_reason: str | None) -> str:
    if blocked_reason == "sector_cap_reached":
        return "CAP"
    if status.lower() == "reserved":
        return "RES"
    if blocked_reason:
        return "BLOCK"
    return "Q"


def _halt_banner(state: str, reason: str | None, backtest_failed: bool) -> str:
    prefix = "WARNING: current algorithm failed backtest after costs; paper trading allowed. " if backtest_failed else ""
    if state == "MARKET_CLOSED":
        return f"{prefix}Market closed - {reason or 'waiting for next open'}"
    if state == "ERROR":
        return f"{prefix}ERROR: {reason or 'operator error'}"
    if state.startswith("HALTED"):
        return f"{prefix}{reason or state}"
    return f"{prefix}{reason or 'Operator running in paper mode'}"


def _realized_pnl(repo: DriftPilotRepository) -> float:
    """Cumulative realized P&L across all closed positions (lifetime)."""
    rows = repo.connection.execute("SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM positions WHERE status = 'closed'").fetchone()
    return float(rows["pnl"] if rows is not None else 0.0)


def _realized_pnl_today(repo: DriftPilotRepository) -> float:
    """Realized P&L from positions closed since UTC midnight today.
    For paper trading, the operator runs in UTC so this maps to a single
    trading session.
    """
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    rows = repo.connection.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM positions "
        "WHERE status = 'closed' AND closed_at >= ?",
        (today_iso,),
    ).fetchone()
    return float(rows["pnl"] if rows is not None else 0.0)


def _trade_count_today(repo: DriftPilotRepository) -> int:
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    rows = repo.connection.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE status = 'closed' AND closed_at >= ?",
        (today_iso,),
    ).fetchone()
    return int(rows["n"] if rows is not None else 0)


def _win_rate(repo: DriftPilotRepository) -> float:
    """Win rate across ALL closed positions (lifetime)."""
    rows = repo.connection.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins FROM positions WHERE status = 'closed'"
    ).fetchone()
    total = int(rows["total"] if rows is not None else 0)
    wins = int(rows["wins"] or 0) if rows is not None else 0
    return wins / total if total else 0.0


def _win_rate_today(repo: DriftPilotRepository) -> float:
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")
    rows = repo.connection.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins "
        "FROM positions WHERE status = 'closed' AND closed_at >= ?",
        (today_iso,),
    ).fetchone()
    total = int(rows["total"] if rows is not None else 0)
    wins = int(rows["wins"] or 0) if rows is not None else 0
    return wins / total if total else 0.0


_ALPACA_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "equity": None,
    "buying_power": None,
    "positions_mv": 0.0,
    "positions_by_symbol": {},
}
_ALPACA_CACHE_TTL = 30.0  # seconds


def _live_alpaca_equity(settings: DriftPilotSettings) -> dict[str, Any] | None:
    """Best-effort live Alpaca equity + buying_power + total mv +
    per-symbol position dict (mark/avg_entry/qty/unrealized). None if
    creds missing or call fails. Cached to avoid hammering the API.
    """
    import time
    if not settings.alpaca_key_id or not settings.alpaca_secret_key:
        return None
    now_t = time.time()
    if _ALPACA_CACHE["equity"] is not None and (now_t - _ALPACA_CACHE["ts"]) < _ALPACA_CACHE_TTL:
        return {
            "equity": _ALPACA_CACHE["equity"],
            "buying_power": _ALPACA_CACHE["buying_power"],
            "positions_mv": _ALPACA_CACHE["positions_mv"],
            "positions_by_symbol": _ALPACA_CACHE["positions_by_symbol"],
        }
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            settings.alpaca_key_id, settings.alpaca_secret_key,
            paper=settings.mode != "live",
        )
        acct = client.get_account()
        positions = client.get_all_positions()
        total_mv = sum(float(p.market_value or 0) for p in positions)
        per_sym: dict[str, dict[str, float]] = {}
        for p in positions:
            try:
                qty = float(p.qty or 0)
                avg = float(p.avg_entry_price or 0)
                mv = float(p.market_value or 0)
                mark = (mv / qty) if qty else avg
                unr_pct = ((mark - avg) / avg) if avg else 0.0
                per_sym[p.symbol.upper()] = {
                    "qty": qty,
                    "avg_entry": avg,
                    "market_value": mv,
                    "mark": mark,
                    "unrealized_pl": float(p.unrealized_pl or 0),
                    "unrealized_pct": unr_pct,
                }
            except (TypeError, ValueError):
                continue
        result = {
            "equity": float(acct.equity or 0),
            "buying_power": float(acct.buying_power or 0),
            "positions_mv": total_mv,
            "positions_by_symbol": per_sym,
        }
        _ALPACA_CACHE.update({"ts": now_t, **result})
        return result
    except Exception:
        return None


def _recent_trades(repo: DriftPilotRepository, limit: int = 20) -> list[dict[str, Any]]:
    """Last N closed positions with full trade chain — symbol, qty, entry,
    exit, P&L, hold time, exit reason, catalyst headline if available.
    Powers the dashboard's RECENT TRADES panel.
    """
    rows = repo.connection.execute(
        "SELECT id, symbol, quantity, entry_price, opened_at, closed_at, "
        "exit_reason, realized_pnl, metadata_json "
        "FROM positions WHERE status = 'closed' "
        "ORDER BY closed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            md = json.loads(r["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            md = {}
        try:
            opened = datetime.fromisoformat(r["opened_at"].replace("Z", "+00:00"))
            closed = datetime.fromisoformat(r["closed_at"].replace("Z", "+00:00"))
            hold_min = (closed - opened).total_seconds() / 60.0
        except Exception:
            hold_min = 0.0
        entry = float(r["entry_price"] or 0)
        exit_price = float(md.get("exit_price") or 0)
        return_pct = ((exit_price - entry) / entry * 100.0) if entry > 0 and exit_price > 0 else 0.0
        out.append({
            "id": r["id"],
            "symbol": r["symbol"],
            "quantity": float(r["quantity"] or 0),
            "entry_price": entry,
            "exit_price": exit_price,
            "return_pct": return_pct,
            "realized_pnl": float(r["realized_pnl"] or 0),
            "hold_minutes": round(hold_min, 1),
            "exit_reason": r["exit_reason"] or "?",
            "closed_at": r["closed_at"],
            "catalyst_headline": (md.get("catalyst_headline") or "")[:100],
            "catalyst_sentiment": md.get("catalyst_sentiment"),
        })
    return out


def _news_ticker(
    db_path: str = "data/driftpilot/catalyst_events.sqlite3",
    limit: int = 30,
    lookback_minutes: int = 240,
) -> list[dict[str, Any]]:
    """Most recent catalyst events for the dashboard scrolling ticker.

    Pulls (symbol, category/subcategory, sentiment, headline, ts) from the
    catalyst sqlite. Independent of the operator's main DB. Best-effort: a
    missing/locked DB just returns []. Limited to last `lookback_minutes`
    so stale headlines don't show up after the operator's been down a day.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone
    p = Path(db_path)
    if not p.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).isoformat()
    try:
        conn = sqlite3.connect(p)
        cur = conn.execute(
            "SELECT symbol, category, subcategory, sentiment, headline, "
            "event_ts, source, priority_modifier "
            "FROM catalyst_events WHERE event_ts >= ? "
            "ORDER BY event_ts DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "symbol": r[0],
            "category": r[1],
            "subcategory": r[2],
            "sentiment": r[3] or "pending",
            "headline": (r[4] or "")[:140],
            "ts": r[5],
            "source": r[6] or "",
            "priority": float(r[7] or 0.0),
        })
    return out


def _equity_curve(starting_capital: float, realized: float) -> list[dict[str, float]]:
    return [
        {"t": index, "equity": starting_capital + (realized * index / 23 if index else 0)}
        for index in range(24)
    ]


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
        "signal": {"name": "intraday_momentum_v1", "version": "1"},
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
        "active_signal": settings.active_signal,
    }
