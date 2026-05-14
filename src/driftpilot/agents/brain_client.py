"""Brain Client — httpx client for the PM Trading Brain on DGX Spark.

Used by PM Agent and Operator to query/store experiences and retrieve
learned skills. Graceful degradation: if brain is unreachable, returns
empty results (system falls back to pre-brain behavior).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("driftpilot.brain_client")

DEFAULT_BRAIN_URL = "http://192.168.1.166:8100"
DEFAULT_TIMEOUT = 5.0  # seconds — brain queries must be fast


@dataclass
class BrainQueryResult:
    """Result from a brain query — similar experiences + active skills."""

    experiences: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, Any]] = field(default_factory=list)
    query_ms: float = 0.0
    is_fallback: bool = False  # True if brain was unreachable


class BrainClient:
    """HTTP client for the PM Trading Brain server on DGX."""

    def __init__(
        self,
        brain_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        enabled: bool | None = None,
    ):
        self.brain_url = brain_url or os.getenv("BRAIN_URL", DEFAULT_BRAIN_URL)
        self.timeout = timeout
        self.enabled = enabled if enabled is not None else os.getenv("BRAIN_ENABLED", "true").lower() == "true"
        self._client = httpx.Client(base_url=self.brain_url, timeout=self.timeout)
        self._healthy = True
        self._consecutive_failures = 0
        self._max_failures_before_backoff = 3

    def is_available(self) -> bool:
        """Check if brain server is reachable."""
        if not self.enabled:
            return False
        try:
            resp = self._client.get("/brain/health")
            resp.raise_for_status()
            self._healthy = True
            self._consecutive_failures = 0
            return True
        except Exception:
            self._healthy = False
            return False

    def query(
        self,
        context: dict[str, Any],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
        include_skills: bool = True,
    ) -> BrainQueryResult:
        """Query brain for similar past experiences and relevant skills.

        Returns empty result (graceful degradation) if brain is unreachable.
        """
        if not self.enabled:
            return BrainQueryResult(is_fallback=True)

        # Circuit breaker: skip if too many consecutive failures
        if self._consecutive_failures >= self._max_failures_before_backoff:
            logger.debug("brain_circuit_open", extra={"failures": self._consecutive_failures})
            return BrainQueryResult(is_fallback=True)

        try:
            start = time.perf_counter()
            resp = self._client.post(
                "/brain/query",
                json={
                    "context": context,
                    "top_k": top_k,
                    "filters": filters,
                    "include_skills": include_skills,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

            self._consecutive_failures = 0
            self._healthy = True

            logger.info(
                "brain_query_ok",
                extra={
                    "experiences": len(data.get("experiences", [])),
                    "skills": len(data.get("skills", [])),
                    "query_ms": elapsed_ms,
                },
            )

            return BrainQueryResult(
                experiences=data.get("experiences", []),
                skills=data.get("skills", []),
                query_ms=elapsed_ms,
            )
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning(
                "brain_query_failed",
                extra={"error": str(e), "consecutive_failures": self._consecutive_failures},
            )
            return BrainQueryResult(is_fallback=True)

    def store(
        self,
        context: dict[str, Any],
        decision: dict[str, Any],
        exp_type: str = "entry_decision",
        outcome: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Store a trading experience. Returns experience_id or None on failure."""
        if not self.enabled:
            return None

        try:
            resp = self._client.post(
                "/brain/store",
                json={
                    "exp_type": exp_type,
                    "context": context,
                    "decision": decision,
                    "outcome": outcome,
                    "metadata": metadata or {},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            exp_id = data.get("experience_id")
            logger.info("brain_store_ok", extra={"experience_id": exp_id})
            return exp_id
        except Exception as e:
            logger.warning("brain_store_failed", extra={"error": str(e)})
            return None

    def backfill(self, experience_id: str, outcome: dict[str, Any]) -> bool:
        """Backfill an experience with its outcome after position closes."""
        if not self.enabled:
            return False

        try:
            resp = self._client.post(
                "/brain/backfill",
                json={"experience_id": experience_id, "outcome": outcome},
            )
            resp.raise_for_status()
            logger.info("brain_backfill_ok", extra={"experience_id": experience_id})
            return True
        except Exception as e:
            logger.warning("brain_backfill_failed", extra={"error": str(e)})
            return False

    def reflect(self, date: str) -> dict[str, Any] | None:
        """Trigger end-of-day reflection. Returns reflection summary or None."""
        if not self.enabled:
            return None

        try:
            resp = self._client.post(
                "/brain/reflect",
                json={"date": date},
                timeout=60.0,  # Reflection can take a while (Qwen analysis)
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "brain_reflect_ok",
                extra={
                    "skills_created": data.get("skills_created", 0),
                    "skills_retired": data.get("skills_retired", 0),
                },
            )
            return data
        except Exception as e:
            logger.warning("brain_reflect_failed", extra={"error": str(e)})
            return None

    def get_skills(self, status: str = "active") -> list[dict[str, Any]]:
        """Get current skills."""
        if not self.enabled:
            return []

        try:
            resp = self._client.get("/brain/skills", params={"status": status})
            resp.raise_for_status()
            return resp.json().get("skills", [])
        except Exception:
            return []

    def get_stats(self) -> dict[str, Any] | None:
        """Get brain health stats."""
        if not self.enabled:
            return None

        try:
            resp = self._client.get("/brain/stats")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def reset_circuit_breaker(self) -> None:
        """Reset the circuit breaker (e.g., after manually fixing connectivity)."""
        self._consecutive_failures = 0
        self._healthy = True

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()


def format_experiences_for_prompt(experiences: list[dict[str, Any]], max_items: int = 5) -> str:
    """Format retrieved experiences into a text block for LLM prompt injection.

    This is the key RAG integration point — turns vector search results
    into natural language context for the PM Agent's Qwen prompt.
    """
    if not experiences:
        return ""

    lines = ["RELEVANT PAST EXPERIENCES (similar situations):"]
    for i, exp in enumerate(experiences[:max_items], 1):
        ctx = exp.get("context", {})
        dec = exp.get("decision", {})
        outcome = exp.get("outcome")

        symbol = ctx.get("symbol", "?")
        signal = ctx.get("signal", "?")
        headline = ctx.get("headline", "")[:80]
        action = dec.get("action", "?")
        similarity = exp.get("similarity", 0)

        line = f"\n[{i}] {symbol} ({signal}), {action}"
        if headline:
            line += f" — {headline}"

        if outcome:
            pnl = outcome.get("pnl_pct", 0)
            exit_reason = outcome.get("exit_reason", "?")
            hold_min = outcome.get("hold_minutes", "?")
            result = "GOOD" if pnl > 0 else "BAD"
            line += f"\n    Result: {pnl:+.1f}% in {hold_min} min ({exit_reason}). {result} entry."
        else:
            line += "\n    Result: pending"

        if reasoning := dec.get("reasoning"):
            line += f"\n    Reasoning: {reasoning[:80]}"

        line += f"\n    (similarity: {similarity:.0%})"
        lines.append(line)

    return "\n".join(lines)


def format_skills_for_prompt(skills: list[dict[str, Any]], max_items: int = 10) -> str:
    """Format active skills into a text block for LLM prompt injection."""
    if not skills:
        return ""

    lines = ["ACTIVE TRADING RULES (learned from experience):"]
    for skill in skills[:max_items]:
        title = skill.get("title", "")
        rule = skill.get("rule", "")
        confidence = skill.get("confidence", 0)
        lines.append(f"- [{confidence:.0%}] {title}: {rule}")

    return "\n".join(lines)
