from __future__ import annotations

from trading_bot.llm.providers.openai_adapter import OpenAIAdapter


class FakeResponses:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return object()


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


def test_openai_health_check_respects_minimum_output_tokens(monkeypatch) -> None:
    client = FakeClient()
    adapter = OpenAIAdapter(model="gpt-test", api_key="test")
    monkeypatch.setattr(adapter, "_client", lambda: client)

    status = adapter.health_check()

    assert status.ok is True
    assert client.responses.kwargs["max_output_tokens"] >= 16

