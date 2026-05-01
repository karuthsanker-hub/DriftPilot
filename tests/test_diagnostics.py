from __future__ import annotations

from trading_bot.diagnostics import run_env_diagnostics


def test_env_diagnostics_validate_formats_without_network(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "PAPER_MODE=true",
                "SUPABASE_URL=https://example.supabase.co",
                "SUPABASE_KEY=test-key",
                "ALPACA_API_KEY=test-key",
                "ALPACA_SECRET_KEY=test-secret",
                "FINNHUB_API_KEY=finnhub",
                "FMP_API_KEY=fmp",
                "ALPACA_BASE_URL=https://paper-api.alpaca.markets",
                "QWEN_BASE_URL=http://localhost:8001/v1",
                "ACTIVE_LLM_PROVIDER=openai",
                "RISK_PER_TRADE_PCT=0.01",
                "MAX_POSITION_PCT=0.20",
                "VIX_PAUSE_THRESHOLD=25",
                "DAILY_LOSS_LIMIT_PCT=-2",
            ]
        )
    )

    results = run_env_diagnostics(env_path, network=False)

    assert all(result.ok for result in results)


def test_env_diagnostics_flags_bad_url_without_network(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "PAPER_MODE=true",
                "SUPABASE_URL=not-a-url",
                "SUPABASE_KEY=test-key",
                "ALPACA_API_KEY=test-key",
                "ALPACA_SECRET_KEY=test-secret",
                "FINNHUB_API_KEY=finnhub",
                "FMP_API_KEY=fmp",
                "ALPACA_BASE_URL=https://paper-api.alpaca.markets",
                "ACTIVE_LLM_PROVIDER=openai",
                "RISK_PER_TRADE_PCT=0.01",
                "MAX_POSITION_PCT=0.20",
                "VIX_PAUSE_THRESHOLD=25",
                "DAILY_LOSS_LIMIT_PCT=-2",
            ]
        )
    )

    results = run_env_diagnostics(env_path, network=False)

    assert any(result.name == "SUPABASE_URL_format" and not result.ok for result in results)
