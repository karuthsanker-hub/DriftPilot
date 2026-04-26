from __future__ import annotations

from trading_bot.llm.providers.claude_adapter import ClaudeAdapter


class FakeMessages:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return object()


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()


def test_claude_adapter_ignores_anthropic_base_url_by_default(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4000")

    adapter = ClaudeAdapter(model="claude-test", api_key="test")

    assert adapter.base_url == "https://api.anthropic.com"


def test_claude_health_check_uses_safe_token_minimum(monkeypatch) -> None:
    client = FakeClient()
    adapter = ClaudeAdapter(model="claude-test", api_key="test")
    monkeypatch.setattr(adapter, "_client", lambda: client)

    status = adapter.health_check()

    assert status.ok is True
    assert client.messages.kwargs["max_tokens"] >= 16

