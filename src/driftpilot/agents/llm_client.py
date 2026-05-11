"""Dual LLM client for agent decisions (Qwen + Claude).

Qwen (local, fast): individual per-tick decisions (entry, exit, target raise)
Claude (API, slow): session-level adaptation, complex reasoning

Both use OpenAI-compatible /v1/chat/completions endpoint.
Includes timeout handling, JSON schema validation, and fallback behavior.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .prompt_loader import PromptConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Result from an LLM call."""

    success: bool
    parsed: dict[str, Any]  # Parsed JSON response
    raw: str = ""  # Raw response text
    model: str = ""
    latency_ms: int = 0
    error: str | None = None
    used_fallback: bool = False


class LLMClient:
    """Dual Qwen + Claude client with timeout and fallback.

    Usage:
        client = LLMClient(qwen_url="http://192.168.1.166:8000/v1")
        response = await client.complete(prompt_config, template_vars)
        if response.success:
            decision = response.parsed["decision"]
    """

    def __init__(
        self,
        qwen_url: str = "http://192.168.1.166:8000/v1",
        qwen_model: str = "Qwen/Qwen3-8B",
        qwen_timeout_ms: int = 500,
        claude_api_key: str = "",
        claude_model: str = "claude-sonnet-4-20250514",
        claude_timeout_ms: int = 3000,
    ) -> None:
        self._qwen_url = qwen_url.rstrip("/")
        self._qwen_model = qwen_model
        self._qwen_timeout_ms = qwen_timeout_ms
        self._claude_api_key = claude_api_key
        self._claude_model = claude_model
        self._claude_timeout_ms = claude_timeout_ms

    def complete(
        self,
        prompt: PromptConfig,
        template_vars: dict[str, Any],
    ) -> LLMResponse:
        """Call the appropriate LLM based on prompt config.

        Returns parsed JSON response or fallback on failure.
        """
        user_content = prompt.render_user(**template_vars)
        model_type = prompt.model.lower()

        if model_type == "qwen":
            return self._call_qwen(prompt, user_content)
        elif model_type == "claude":
            return self._call_claude(prompt, user_content)
        else:
            logger.error("Unknown model type: %s", model_type)
            return self._fallback_response(prompt, "unknown_model_type")

    def _call_qwen(self, prompt: PromptConfig, user_content: str) -> LLMResponse:
        """Call local Qwen via OpenAI-compatible API."""
        timeout_s = prompt.timeout_ms / 1000.0
        url = f"{self._qwen_url}/chat/completions"

        body = {
            "model": self._qwen_model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": prompt.max_tokens,
            "temperature": prompt.temperature,
        }

        start = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(url, json=body)
                resp.raise_for_status()
        except httpx.TimeoutException:
            latency = int((time.perf_counter() - start) * 1000)
            logger.warning("Qwen timeout after %dms", latency)
            return self._fallback_response(prompt, f"timeout_{latency}ms")
        except httpx.HTTPError as exc:
            latency = int((time.perf_counter() - start) * 1000)
            logger.warning("Qwen HTTP error: %s", exc)
            return self._fallback_response(prompt, str(exc))

        latency = int((time.perf_counter() - start) * 1000)
        data = resp.json()
        raw_text = data["choices"][0]["message"]["content"]

        return self._parse_response(raw_text, self._qwen_model, latency, prompt)

    def _call_claude(self, prompt: PromptConfig, user_content: str) -> LLMResponse:
        """Call Claude via Anthropic Messages API."""
        if not self._claude_api_key:
            return self._fallback_response(prompt, "no_claude_api_key")

        timeout_s = prompt.timeout_ms / 1000.0
        url = "https://api.anthropic.com/v1/messages"

        headers = {
            "x-api-key": self._claude_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self._claude_model,
            "max_tokens": prompt.max_tokens,
            "system": prompt.system,
            "messages": [{"role": "user", "content": user_content}],
            "temperature": prompt.temperature,
        }

        start = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(url, json=body, headers=headers)
                resp.raise_for_status()
        except httpx.TimeoutException:
            latency = int((time.perf_counter() - start) * 1000)
            logger.warning("Claude timeout after %dms", latency)
            return self._fallback_response(prompt, f"timeout_{latency}ms")
        except httpx.HTTPError as exc:
            latency = int((time.perf_counter() - start) * 1000)
            logger.warning("Claude HTTP error: %s", exc)
            return self._fallback_response(prompt, str(exc))

        latency = int((time.perf_counter() - start) * 1000)
        data = resp.json()
        raw_text = data["content"][0]["text"]

        return self._parse_response(raw_text, self._claude_model, latency, prompt)

    def _parse_response(
        self,
        raw_text: str,
        model: str,
        latency_ms: int,
        prompt: PromptConfig,
    ) -> LLMResponse:
        """Parse LLM response as JSON. Falls back on parse failure."""
        # Strip markdown code fences if present
        text = raw_text.strip()
        if text.startswith("```"):
            # Remove opening fence (possibly with language tag)
            first_newline = text.index("\n")
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()

        # Handle /no_think responses (Qwen reasoning mode)
        if "<think>" in text:
            think_end = text.find("</think>")
            if think_end != -1:
                text = text[think_end + 8:].strip()

        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError(f"Expected dict, got {type(parsed).__name__}")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "LLM JSON parse failed (model=%s): %s | raw: %s",
                model,
                exc,
                raw_text[:200],
            )
            return self._fallback_response(
                prompt, f"json_parse_error: {exc}", raw=raw_text, latency_ms=latency_ms
            )

        return LLMResponse(
            success=True,
            parsed=parsed,
            raw=raw_text,
            model=model,
            latency_ms=latency_ms,
        )

    def _fallback_response(
        self,
        prompt: PromptConfig,
        error: str,
        *,
        raw: str = "",
        latency_ms: int = 0,
    ) -> LLMResponse:
        """Generate fallback response when LLM fails."""
        fallback_action = prompt.fallback_action
        logger.info(
            "Using fallback action '%s' for prompt '%s' due to: %s",
            fallback_action,
            prompt.name,
            error,
        )

        # Build a minimal valid response based on fallback_action
        parsed: dict[str, Any]
        if fallback_action == "approve":
            parsed = {
                "decision": "approve",
                "reasoning": f"fallback: {error}",
                "target_pct": 0.01,
                "size_multiplier": 1.0,
            }
        elif fallback_action == "deny":
            parsed = {
                "decision": "deny",
                "reasoning": f"fallback: {error}",
            }
        elif fallback_action == "hold":
            parsed = {
                "action": "hold",
                "reasoning": f"fallback: {error}",
                "confidence": 0.0,
            }
        elif fallback_action == "follow_algo":
            parsed = {
                "action": "follow_algo",
                "reasoning": f"fallback: {error}",
            }
        else:
            parsed = {"action": fallback_action, "reasoning": f"fallback: {error}"}

        return LLMResponse(
            success=False,
            parsed=parsed,
            raw=raw,
            model="fallback",
            latency_ms=latency_ms,
            error=error,
            used_fallback=True,
        )
