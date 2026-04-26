from __future__ import annotations

from trading_bot.settings import load_settings


def test_settings_loads_pead_scan_tickers(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("PEAD_SCAN_TICKERS=abc, msft,NVDA\n")
    monkeypatch.delenv("PEAD_SCAN_TICKERS", raising=False)

    settings = load_settings(env_path)

    assert settings.pead_scan_tickers == ["ABC", "MSFT", "NVDA"]
