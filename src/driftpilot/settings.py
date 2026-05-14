from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


DEFAULT_SQLITE_PATH = "data/driftpilot/operator_state.sqlite3"
DEFAULT_TIMEZONE = "America/New_York"


def _env_values(env_path: str | Path | None, environ: Mapping[str, str] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_path is not None:
        values.update({key: value for key, value in dotenv_values(env_path).items() if value is not None})
    if environ is not None:
        values.update(dict(environ))
    else:
        import os

        values.update(dict(os.environ))
    return values


def _get_str(values: Mapping[str, str], key: str, default: str) -> str:
    value = values.get(key)
    if value is None or value == "":
        return default
    return value


def _get_int(values: Mapping[str, str], key: str, default: int) -> int:
    value = values.get(key)
    if value is None or value == "":
        return default
    return int(value)


def _get_float(values: Mapping[str, str], key: str, default: float) -> float:
    value = values.get(key)
    if value is None or value == "":
        return default
    return float(value)


def _get_bool(values: Mapping[str, str], key: str, default: bool) -> bool:
    value = values.get(key)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _get_positive_float(values: Mapping[str, str], key: str, default: float) -> float:
    value = _get_float(values, key, default)
    return value if value > 0 else default


@dataclass(frozen=True, slots=True)
class DriftPilotSettings:
    mode: str = "paper"
    live_ok: bool = False
    sqlite_path: str = DEFAULT_SQLITE_PATH
    timezone: str = DEFAULT_TIMEZONE
    paper_capital: float = 10_000.0
    trade_slots: int = 10
    slot_value: float = 1_000.0
    target_pct: float = 0.01
    stop_pct: float = 0.01
    max_hold_minutes: int = 45
    scan_interval_seconds: int = 30
    entry_limit_timeout_seconds: int = 30
    exit_limit_timeout_seconds: int = 15
    spy_stale_seconds: int = 60
    always_on_candidate_count: int = 50
    max_trades_per_day: int = 50
    max_trades_per_symbol_per_day: int = 3
    consecutive_loss_limit: int = 2
    daily_loss_limit_pct: float = 0.03
    equity_floor: float = 26_000.0
    live_equity_buffer: float = 1_000.0
    backtest_expectancy_passed: bool = False
    paper_trading_gate_passed: bool = False
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_live_base_url: str = "https://api.alpaca.markets"
    alpaca_data_feed: str = "sip"
    universe_file: str = "config/universe.csv"
    parquet_bar_root: str = "data/bars/databento"
    active_signal: str = "intraday_momentum_v1"
    # v3 catalyst layer
    catalyst_enabled: bool = False
    catalyst_db_path: str = "data/driftpilot/catalyst_events.sqlite3"
    catalyst_qwen_url: str = "http://192.168.1.166:8000/v1"
    catalyst_qwen_model: str = "Qwen/Qwen3-8B"
    catalyst_qwen_timeout_ms: int = 3000
    catalyst_rss_enabled: bool = True
    catalyst_alpaca_poll_seconds: int = 30
    catalyst_universe_lookback_minutes: int = 240
    # Agent layer
    agent_enabled: bool = False
    agent_num_slots: int = 10
    agent_qwen_url: str = "http://192.168.1.166:8000/v1"
    agent_qwen_model: str = "Qwen/Qwen3-8B"
    agent_qwen_timeout_ms: int = 500
    agent_claude_api_key: str = ""
    agent_claude_model: str = "claude-sonnet-4-20250514"
    agent_claude_timeout_ms: int = 3000
    agent_max_override_rate: float = 0.20
    agent_prompts_dir: str = "config/prompts"
    agent_db_path: str = "data/driftpilot/agent_messages.sqlite3"
    agent_message_ttl_seconds: int = 300

    @property
    def sqlite_path_obj(self) -> Path:
        return Path(self.sqlite_path)


def load_settings(
    env_path: str | Path | None = ".env",
    *,
    environ: Mapping[str, str] | None = None,
) -> DriftPilotSettings:
    values = _env_values(env_path, environ)

    return DriftPilotSettings(
        mode=_get_str(values, "MODE", "paper").lower(),
        live_ok=_get_bool(values, "LIVE_OK", False),
        sqlite_path=_get_str(values, "DRIFTPILOT_SQLITE_PATH", DEFAULT_SQLITE_PATH),
        timezone=_get_str(values, "DRIFTPILOT_TIMEZONE", DEFAULT_TIMEZONE),
        paper_capital=_get_float(values, "OPERATOR_PAPER_CAPITAL", 10_000.0),
        trade_slots=_get_int(values, "OPERATOR_TRADE_SLOTS", 10),
        slot_value=_get_float(values, "OPERATOR_SLOT_VALUE", 1_000.0),
        target_pct=_get_float(values, "OPERATOR_TARGET_PCT", 0.01),
        stop_pct=_get_float(values, "OPERATOR_STOP_PCT", 0.01),
        max_hold_minutes=_get_int(values, "MAX_HOLD_MINUTES", 45),
        scan_interval_seconds=_get_int(values, "SCAN_INTERVAL_SECONDS", 30),
        entry_limit_timeout_seconds=_get_int(values, "ENTRY_LIMIT_TIMEOUT_SECONDS", 30),
        exit_limit_timeout_seconds=_get_int(values, "EXIT_LIMIT_TIMEOUT_SECONDS", 15),
        spy_stale_seconds=_get_int(values, "SPY_STALE_SECONDS", 60),
        always_on_candidate_count=_get_int(values, "ALWAYS_ON_CANDIDATE_COUNT", 50),
        max_trades_per_day=_get_int(values, "MAX_TRADES_PER_DAY", 50),
        max_trades_per_symbol_per_day=_get_int(values, "MAX_TRADES_PER_SYMBOL_PER_DAY", 3),
        consecutive_loss_limit=_get_int(values, "CONSECUTIVE_LOSS_LIMIT", 2),
        daily_loss_limit_pct=_get_positive_float(values, "DAILY_LOSS_LIMIT_PCT", 0.03),
        equity_floor=_get_float(values, "EQUITY_FLOOR", 26_000.0),
        live_equity_buffer=_get_float(values, "LIVE_EQUITY_BUFFER", 1_000.0),
        backtest_expectancy_passed=_get_bool(values, "BACKTEST_EXPECTANCY_PASSED", False),
        paper_trading_gate_passed=_get_bool(values, "PAPER_TRADING_GATE_PASSED", False),
        # Accept either ALPACA_KEY_ID (canonical) or ALPACA_API_KEY (alpaca-py
        # docs naming) — both are common in the wild.
        alpaca_key_id=(
            _get_str(values, "ALPACA_KEY_ID", "")
            or _get_str(values, "ALPACA_API_KEY", "")
        ),
        alpaca_secret_key=_get_str(values, "ALPACA_SECRET_KEY", ""),
        alpaca_paper_base_url=_get_str(
            values,
            "ALPACA_PAPER_BASE_URL",
            "https://paper-api.alpaca.markets",
        ),
        alpaca_live_base_url=_get_str(values, "ALPACA_LIVE_BASE_URL", "https://api.alpaca.markets"),
        alpaca_data_feed=_get_str(values, "ALPACA_DATA_FEED", "sip"),
        universe_file=_get_str(values, "DRIFTPILOT_UNIVERSE_FILE", "config/universe.csv"),
        parquet_bar_root=_get_str(values, "DRIFTPILOT_PARQUET_BAR_ROOT", "data/bars/databento"),
        active_signal=_get_str(values, "ACTIVE_SIGNAL", "intraday_momentum_v1"),
        catalyst_enabled=_get_bool(values, "CATALYST_ENABLED", False),
        catalyst_db_path=_get_str(values, "CATALYST_DB_PATH", "data/driftpilot/catalyst_events.sqlite3"),
        catalyst_qwen_url=_get_str(values, "CATALYST_QWEN_URL", "http://192.168.1.166:8000/v1"),
        catalyst_qwen_model=_get_str(values, "CATALYST_QWEN_MODEL", "Qwen/Qwen3-8B"),
        catalyst_qwen_timeout_ms=_get_int(values, "CATALYST_QWEN_TIMEOUT_MS", 3000),
        catalyst_rss_enabled=_get_bool(values, "CATALYST_RSS_ENABLED", True),
        catalyst_alpaca_poll_seconds=_get_int(values, "CATALYST_ALPACA_POLL_SECONDS", 30),
        catalyst_universe_lookback_minutes=_get_int(values, "CATALYST_UNIVERSE_LOOKBACK_MINUTES", 240),
        agent_enabled=_get_bool(values, "AGENT_ENABLED", False),
        agent_num_slots=_get_int(values, "AGENT_NUM_SLOTS", 10),
        agent_qwen_url=_get_str(values, "AGENT_QWEN_URL", "http://192.168.1.166:8000/v1"),
        agent_qwen_model=_get_str(values, "AGENT_QWEN_MODEL", "Qwen/Qwen3-8B"),
        agent_qwen_timeout_ms=_get_int(values, "AGENT_QWEN_TIMEOUT_MS", 500),
        agent_claude_api_key=_get_str(values, "AGENT_CLAUDE_API_KEY", ""),
        agent_claude_model=_get_str(values, "AGENT_CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        agent_claude_timeout_ms=_get_int(values, "AGENT_CLAUDE_TIMEOUT_MS", 3000),
        agent_max_override_rate=_get_float(values, "AGENT_MAX_OVERRIDE_RATE", 0.20),
        agent_prompts_dir=_get_str(values, "AGENT_PROMPTS_DIR", "config/prompts"),
        agent_db_path=_get_str(values, "AGENT_DB_PATH", "data/driftpilot/agent_messages.sqlite3"),
        agent_message_ttl_seconds=_get_int(values, "AGENT_MESSAGE_TTL_SECONDS", 300),
    )
