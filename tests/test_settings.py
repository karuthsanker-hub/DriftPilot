from __future__ import annotations

from trading_bot.settings import load_settings
from trading_bot.universe import load_pead_universe


def test_settings_loads_pead_scan_tickers(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("PEAD_SCAN_TICKERS=abc, msft,NVDA\n")
    monkeypatch.delenv("PEAD_SCAN_TICKERS", raising=False)

    settings = load_settings(env_path)

    assert settings.pead_scan_tickers == ["ABC", "MSFT", "NVDA"]


def test_settings_loads_earnings_events_file(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EARNINGS_EVENTS_FILE=data/earnings.csv\n")
    monkeypatch.delenv("EARNINGS_EVENTS_FILE", raising=False)

    settings = load_settings(env_path)

    assert settings.earnings_events_file == "data/earnings.csv"


def test_settings_loads_diagram_exit_parameters(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("PEAD_TARGET_PCT=0.08\nPEAD_STOP_PCT=0.04\nPEAD_MAX_HOLD_DAYS=20\n")
    monkeypatch.delenv("PEAD_TARGET_PCT", raising=False)
    monkeypatch.delenv("PEAD_STOP_PCT", raising=False)
    monkeypatch.delenv("PEAD_MAX_HOLD_DAYS", raising=False)

    settings = load_settings(env_path)

    assert settings.pead_target_pct == 0.08
    assert settings.pead_stop_pct == 0.04
    assert settings.pead_max_hold_days == 20


def test_settings_defaults_to_finbert_for_diagram_flow(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("")
    monkeypatch.delenv("PEAD_SENTIMENT", raising=False)

    settings = load_settings(env_path)

    assert settings.pead_sentiment == "finbert"


def test_settings_loads_operator_capital_and_exit_plan(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPERATOR_PAPER_CAPITAL=10000\n"
        "OPERATOR_TARGET_PCT=0.01\n"
        "OPERATOR_STOP_PCT=0.01\n"
        "OPERATOR_MAX_CANDIDATES=100\n"
        "OPERATOR_TRADE_SLOTS=10\n"
        "OPERATOR_MIN_CANDIDATES=5\n"
        "OPERATOR_REFRESH_BATCH_SIZE=1\n"
        "OPERATOR_REFRESH_INTERVAL_MINUTES=5\n"
        "OPERATOR_UNIVERSE_REFRESH_INTERVAL_MINUTES=6\n"
        "OPERATOR_MONITOR_INTERVAL_MINUTES=4\n"
        "MARKET_DATA_RETRY_ATTEMPTS=4\n"
        "MARKET_DATA_RETRY_BACKOFF_SECONDS=0.25\n"
    )
    monkeypatch.delenv("OPERATOR_PAPER_CAPITAL", raising=False)
    monkeypatch.delenv("OPERATOR_TARGET_PCT", raising=False)
    monkeypatch.delenv("OPERATOR_STOP_PCT", raising=False)
    monkeypatch.delenv("OPERATOR_MAX_CANDIDATES", raising=False)
    monkeypatch.delenv("OPERATOR_TRADE_SLOTS", raising=False)
    monkeypatch.delenv("OPERATOR_MIN_CANDIDATES", raising=False)
    monkeypatch.delenv("OPERATOR_REFRESH_BATCH_SIZE", raising=False)
    monkeypatch.delenv("OPERATOR_REFRESH_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("OPERATOR_UNIVERSE_REFRESH_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("OPERATOR_MONITOR_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("MARKET_DATA_RETRY_ATTEMPTS", raising=False)
    monkeypatch.delenv("MARKET_DATA_RETRY_BACKOFF_SECONDS", raising=False)

    settings = load_settings(env_path)

    assert settings.operator_paper_capital == 10_000
    assert settings.operator_target_pct == 0.01
    assert settings.operator_stop_pct == 0.01
    assert settings.operator_max_candidates == 100
    assert settings.operator_trade_slots == 10
    assert settings.operator_min_candidates == 5
    assert settings.operator_refresh_batch_size == 1
    assert settings.operator_refresh_interval_minutes == 5
    assert settings.operator_universe_refresh_interval_minutes == 6
    assert settings.operator_monitor_interval_minutes == 4
    assert settings.market_data_retry_attempts == 4
    assert settings.market_data_retry_backoff_seconds == 0.25


def test_settings_loads_pead_universe_file_when_env_override_empty(tmp_path, monkeypatch) -> None:
    universe = tmp_path / "universe.csv"
    universe.write_text("ticker,name\nabc,Acme\nMSFT,Microsoft\nabc,Duplicate\n")
    env_path = tmp_path / ".env"
    env_path.write_text(f"PEAD_UNIVERSE_FILE={universe.name}\nPEAD_SCAN_TICKERS=\n")
    monkeypatch.delenv("PEAD_SCAN_TICKERS", raising=False)
    monkeypatch.delenv("PEAD_UNIVERSE_FILE", raising=False)

    settings = load_settings(env_path)

    assert load_pead_universe(settings, env_path=env_path) == ["ABC", "MSFT"]
