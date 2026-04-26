from __future__ import annotations

from fastapi.testclient import TestClient

from trading_bot.dashboard.app import create_app


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
    assert "Trading Bot Dashboard" in response.text


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
