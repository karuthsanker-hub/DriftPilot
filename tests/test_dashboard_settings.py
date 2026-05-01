from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from trading_bot.data.market_data import CompanyProfile, EarningsEvent
from trading_bot.dashboard.app import create_app
from trading_bot.strategies.pead import PEADAction, PEADSignal


def test_dashboard_saves_provider_settings_to_env(tmp_path) -> None:
    env_path = tmp_path / ".env"
    client = TestClient(create_app(env_path))

    response = client.post(
        "/settings",
        json={
            "active_provider": "qwen",
            "openai_model": "gpt-4.1",
            "claude_model": "claude-sonnet-4-20250514",
            "gemini_model": "gemini-2.5-pro",
            "qwen_base_url": "http://localhost:8001/v1/",
            "qwen_model": "qwen-local",
            "openai_api_key": "sk-test",
        },
    )

    assert response.status_code == 200
    assert response.json()["active_provider"] == "qwen"
    text = env_path.read_text()
    assert "ACTIVE_LLM_PROVIDER=qwen" in text
    assert "OPENAI_API_KEY=sk-test" in text
    assert "QWEN_BASE_URL=http://localhost:8001/v1" in text


def test_dashboard_root_renders(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/")

    assert response.status_code == 200
    assert "DriftPilot" in response.text


def test_dashboard_admin_renders(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/admin")

    assert response.status_code == 200
    assert "DriftPilot Admin" in response.text


def test_dashboard_favicon_is_not_noisy_404(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/favicon.ico")

    assert response.status_code == 204


def test_dashboard_reports_missing_provider_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/settings/providers/openai/health")

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["configured"] is False


def test_dashboard_diagnostics_endpoint(tmp_path, monkeypatch) -> None:
    class Result:
        name = "paper_mode"
        ok = True
        message = "set"

    monkeypatch.setattr("trading_bot.dashboard.app.run_env_diagnostics", lambda *_args, **_kwargs: [Result()])
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/api/diagnostics")

    assert response.status_code == 200
    assert response.json() == [{"name": "paper_mode", "ok": True, "message": "set"}]


def test_dashboard_scan_pead_endpoint(tmp_path, monkeypatch) -> None:
    class Result:
        ticker = "ABC"
        persisted = False
        signal = PEADSignal(ticker="ABC", action=PEADAction.SKIP, surprise_pct=0, skip_reason="test")

    class FakeScanner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def scan(self, tickers, scan_date, persist_skips=False):
            assert tickers == ["ABC"]
            return [Result()]

    monkeypatch.setattr("trading_bot.dashboard.app.PEADScanner", FakeScanner)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.post("/api/scan-pead", json={"tickers": "ABC", "persist": False})

    assert response.status_code == 200
    assert response.json()[0]["ticker"] == "ABC"
    assert "entry_price" in response.json()[0]


def test_dashboard_scan_pead_date_range(tmp_path, monkeypatch) -> None:
    seen_dates = []

    class FakeScanner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def scan(self, tickers, scan_date, persist_skips=False):
            seen_dates.append(scan_date.isoformat())
            return []

    monkeypatch.setattr("trading_bot.dashboard.app.PEADScanner", FakeScanner)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.post(
        "/api/scan-pead",
        json={"tickers": "ABC", "persist": False, "start_date": "2026-04-25", "end_date": "2026-04-27"},
    )

    assert response.status_code == 200
    assert seen_dates == ["2026-04-25", "2026-04-26", "2026-04-27"]


def test_dashboard_execute_pending_endpoint(tmp_path, monkeypatch) -> None:
    class Summary:
        attempted = 1
        submitted = 0
        blocked_reason = ""

    class FakeEngine:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def execute_pending_watchlist(self, *, dry_run=True, **_kwargs):
            assert dry_run is True
            return Summary()

    class FakeClient:
        pass

    class FakeTradingRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_daily_summaries(self, *, limit=1):
            return []

        def upsert_daily_summary(self, payload):
            return payload

    class FakeConfigRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def is_trading_active(self):
            return True

    class FakeMacro:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def current_vix(self):
            return 18

    class FakeMarketData:
        def spy_premarket_change_pct(self):
            return 0

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeTradingRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.StrategyConfigRepository", FakeConfigRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.FredMacroDataProvider", FakeMacro)
    monkeypatch.setattr("trading_bot.dashboard.app.create_market_data_provider", lambda *_args, **_kwargs: FakeMarketData())
    monkeypatch.setattr("trading_bot.dashboard.app.PaperExecutionEngine", FakeEngine)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.post("/api/execute-pending", json={"submit": False})

    assert response.status_code == 200
    assert response.json()["attempted"] == 1


def test_dashboard_scheduler_endpoints(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / ".env"))

    status = client.get("/api/scheduler")
    momentum = client.post("/api/scheduler/run/weekly_momentum_scan")

    assert status.status_code == 200
    assert {job["id"] for job in status.json()["jobs"]} == {
        "daily_pead_scan",
        "pending_entry_scan",
        "position_management",
        "weekly_momentum_scan",
        "operator_candidate_refresh",
        "operator_universe_refresh",
        "realtime_entry_monitor",
        "realtime_exit_monitor",
    }
    assert momentum.status_code in {200, 503}


def test_dashboard_health_endpoint(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "x-request-id" in response.headers


def test_dashboard_operator_top_bets_endpoint(tmp_path, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_candidate_watchlist(self):
            return [{"id": "1", "ticker": "ABC", "strategy": "PEAD_LONG", "status": "pending", "entry_price": 100, "surprise_pct": 8}]

        def list_entered_watchlist(self):
            return []

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeRepo)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/api/operator/top-bets")

    assert response.status_code == 200
    assert response.json()["paper_capital"] == 10000
    assert response.json()["candidates"][0]["target_price"] == 101


def test_dashboard_operator_approve_paper_trades_endpoint(tmp_path, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            self.updated = []

        def list_watchlist_by_ids(self, ids):
            return [{"id": "1", "ticker": "ABC", "strategy": "PEAD_LONG", "status": "pending", "entry_price": 100, "surprise_pct": 8}]

        def update_watchlist_trade_plan(self, *_args, **_kwargs):
            return None

        def mark_watchlist_status(self, *_args, **_kwargs):
            return None

        def list_entered_watchlist(self):
            return []

    class FakeConfigRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def is_trading_active(self):
            return True

    class FakeBroker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def submit_market_order(self, intent, *, dry_run=True):
            from trading_bot.execution.alpaca_broker import OrderResult

            return OrderResult(intent.ticker, intent.side, intent.shares, not dry_run, "ok", "order-1")

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.StrategyConfigRepository", FakeConfigRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.AlpacaBroker", FakeBroker)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.post("/api/operator/approve-paper-trades", json={"selected_ids": ["1"], "submit": True})

    assert response.status_code == 200
    assert response.json()["attempted"] == 1
    assert response.json()["submitted"][0]["submitted"] is True


def test_dashboard_operator_open_positions_endpoint_shows_exit_status(tmp_path, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_entered_watchlist(self):
            return [
                {
                    "id": "2",
                    "ticker": "ABC",
                    "strategy": "PEAD_LONG",
                    "status": "entered",
                    "entry_price": 100,
                    "target_price": 101,
                    "stop_loss": 99,
                    "shares": 10,
                    "position_value": 1000,
                }
            ]

    class FakeConfigRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def is_trading_active(self):
            return True

    class FakeMarketData:
        def company_profile(self, ticker: str):
            return CompanyProfile(ticker=ticker, market_cap_m=100, analyst_count=1, current_price=101.5, avg_volume=1000)

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.StrategyConfigRepository", FakeConfigRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.create_market_data_provider", lambda *_args, **_kwargs: FakeMarketData())
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/api/operator/open-positions")

    assert response.status_code == 200
    assert response.json()["exit_ready"] == 1
    assert response.json()["positions"][0]["action"] == "exit_profit"
    assert response.json()["positions"][0]["unrealized_pnl"] == 15


def test_dashboard_operator_performance_endpoint_splits_realized_and_unrealized(tmp_path, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_recent_trades(self, *, limit: int = 500):
            return [
                {"ticker": "WIN", "strategy": "MOMENTUM", "side": "sell", "entry_price": 100, "exit_price": 110, "shares": 2},
                {"ticker": "LOSS", "strategy": "MOMENTUM", "side": "sell", "entry_price": 50, "exit_price": 45, "shares": 1},
                {"ticker": "OLD", "strategy": "MOMENTUM", "side": "sell", "shares": 1, "exit_reason": "stop"},
            ]

        def list_entered_watchlist(self):
            return [
                {
                    "id": "2",
                    "ticker": "OPEN",
                    "strategy": "PEAD_LONG",
                    "status": "entered",
                    "entry_price": 100,
                    "target_price": 110,
                    "stop_loss": 90,
                    "shares": 1,
                    "position_value": 100,
                }
            ]

    class FakeConfigRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def is_trading_active(self):
            return True

    class FakeMarketData:
        def company_profile(self, ticker: str):
            return CompanyProfile(ticker=ticker, market_cap_m=100, analyst_count=1, current_price=103, avg_volume=1000)

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.StrategyConfigRepository", FakeConfigRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.create_market_data_provider", lambda *_args, **_kwargs: FakeMarketData())
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/api/operator/performance")

    assert response.status_code == 200
    assert response.json()["realized_profit"] == 20
    assert response.json()["realized_loss"] == 5
    assert response.json()["realized_pnl"] == 15
    assert response.json()["unrealized_pnl"] == 3
    assert response.json()["total_pnl"] == 18
    assert response.json()["unpriced_exit_count"] == 1


def test_dashboard_reset_paper_state_endpoint_resets_database_and_broker(tmp_path, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def reset_operator_paper_state(self):
            return {"deleted_trades": 2, "deleted_watchlist": 3}

    class FakeBroker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def reset_paper_account(self):
            return {"canceled_orders": 1, "closed_positions": 1}

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeRepo)
    monkeypatch.setattr("trading_bot.dashboard.app.AlpacaBroker", FakeBroker)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.post("/api/operator/reset-paper-state", json={"reset_broker": True})

    assert response.status_code == 200
    assert response.json()["status"] == "reset"
    assert response.json()["database"]["deleted_trades"] == 2
    assert response.json()["broker"]["closed_positions"] == 1
    assert response.json()["scheduler_running"] is True


def test_dashboard_pead_universe_endpoint_reads_configured_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PEAD_SCAN_TICKERS", raising=False)
    monkeypatch.delenv("PEAD_UNIVERSE_FILE", raising=False)
    universe = tmp_path / "universe.csv"
    universe.write_text("ticker\nAAPL\nMSFT\n")
    env_path = tmp_path / ".env"
    env_path.write_text(f"SUPABASE_URL=https://example.supabase.co\nSUPABASE_KEY=test-key\nPEAD_UNIVERSE_FILE={universe.name}\n")
    client = TestClient(create_app(env_path))

    response = client.get("/api/universe/pead")

    assert response.status_code == 200
    assert response.json()["tickers"] == ["AAPL", "MSFT"]


def test_dashboard_earnings_events_endpoint_reads_local_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PEAD_SCAN_TICKERS", raising=False)
    monkeypatch.delenv("PEAD_UNIVERSE_FILE", raising=False)
    monkeypatch.delenv("EARNINGS_EVENTS_FILE", raising=False)
    universe = tmp_path / "universe.csv"
    universe.write_text("ticker\nABC\n")
    earnings = tmp_path / "earnings.csv"
    earnings.write_text("ticker,earnings_date,actual_eps,estimate_eps,text\nABC,2026-04-24,1.2,1.0,beat\n")
    env_path = tmp_path / ".env"
    env_path.write_text(f"PEAD_UNIVERSE_FILE={universe.name}\nEARNINGS_EVENTS_FILE={earnings.name}\n")
    client = TestClient(create_app(env_path))

    response = client.get("/api/earnings-events")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["events"][0]["ticker"] == "ABC"


def test_dashboard_earnings_events_import_endpoint(tmp_path, monkeypatch) -> None:
    class FakeProvider:
        def latest_earnings_event(self, ticker: str, scan_date: date) -> EarningsEvent:
            return EarningsEvent(ticker=ticker, earnings_date=scan_date, actual_eps=1.2, estimate_eps=1.0, text="beat")

    monkeypatch.delenv("PEAD_SCAN_TICKERS", raising=False)
    monkeypatch.delenv("PEAD_UNIVERSE_FILE", raising=False)
    monkeypatch.delenv("EARNINGS_EVENTS_FILE", raising=False)
    universe = tmp_path / "universe.csv"
    universe.write_text("ticker\nABC\n")
    env_path = tmp_path / ".env"
    env_path.write_text(f"PEAD_UNIVERSE_FILE={universe.name}\nEARNINGS_EVENTS_FILE=earnings.csv\n")
    monkeypatch.setattr("trading_bot.dashboard.app.create_market_data_provider", lambda *_args, **_kwargs: FakeProvider())
    client = TestClient(create_app(env_path))

    response = client.post("/api/earnings-events/import")

    assert response.status_code == 200
    assert response.json()["source"] == "finnhub"
    assert response.json()["imported"] == 1


def test_dashboard_momentum_scores_endpoint(tmp_path, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_recent_momentum_scores(self, *, limit: int = 50):
            assert limit == 50
            return [{"ticker": "AMZN", "total_score": 4}]

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeRepo)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.get("/api/momentum-scores")

    assert response.status_code == 200
    assert response.json() == [{"ticker": "AMZN", "total_score": 4}]


def test_dashboard_portfolio_data_endpoints(tmp_path, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_recent_trades(self, *, limit: int = 50):
            return [{"ticker": "ABC", "pnl": 12.5}]

        def list_daily_summaries(self, *, limit: int = 20):
            return [{"date": "2026-04-26", "pnl": 12.5}]

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeRepo)
    client = TestClient(create_app(tmp_path / ".env"))

    trades = client.get("/api/trades")
    summaries = client.get("/api/daily-summaries")

    assert trades.status_code == 200
    assert summaries.status_code == 200
    assert trades.json()[0]["ticker"] == "ABC"
    assert summaries.json()[0]["date"] == "2026-04-26"


def test_dashboard_backtest_trades_endpoint(tmp_path, monkeypatch) -> None:
    class FakeRepo:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_recent_trades(self, *, limit: int = 500):
            return [{"ticker": "ABC", "side": "buy", "entry_price": 100, "exit_price": 110, "shares": 10}]

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("trading_bot.dashboard.app.TradingRepository", FakeRepo)
    client = TestClient(create_app(tmp_path / ".env"))

    response = client.post("/api/backtest/trades", json={"starting_equity": 10000, "transaction_cost_bps": 0})

    assert response.status_code == 200
    assert response.json()["trade_count"] == 1
    assert response.json()["total_pnl"] == 100
