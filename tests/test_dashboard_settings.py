from __future__ import annotations

from fastapi.testclient import TestClient

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

        def execute_pending_watchlist(self, *, dry_run=True):
            assert dry_run is True
            return Summary()

    class FakeClient:
        pass

    monkeypatch.setattr("trading_bot.dashboard.app.create_supabase_client", lambda *_args, **_kwargs: FakeClient())
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
    assert {job["id"] for job in status.json()["jobs"]} == {"daily_pead_scan", "pending_entry_scan", "weekly_momentum_scan"}
    assert momentum.status_code == 200
    assert momentum.json()["status"] == "not_implemented"
