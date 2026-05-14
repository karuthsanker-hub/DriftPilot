"""Factory for creating the agent orchestrator from DriftPilot settings.

Usage:
    from driftpilot.agents.factory import build_orchestrator
    from driftpilot.settings import load_settings

    settings = load_settings()
    orch = build_orchestrator(settings)
    orch.start()
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .orchestrator import AgentOrchestrator, OrchestratorConfig

if TYPE_CHECKING:
    from driftpilot.settings import DriftPilotSettings

logger = logging.getLogger(__name__)


def build_orchestrator(settings: DriftPilotSettings) -> AgentOrchestrator:
    """Create an AgentOrchestrator from DriftPilot settings.

    Returns a configured orchestrator. Call ``orch.start()`` to activate.
    If ``settings.agent_enabled`` is False, the orchestrator will be a
    no-op — ``start()`` returns immediately, all ticks return zero.
    """
    config = OrchestratorConfig(
        enabled=settings.agent_enabled,
        num_slots=settings.agent_num_slots,
        qwen_url=settings.agent_qwen_url,
        qwen_model=settings.agent_qwen_model,
        qwen_timeout_ms=settings.agent_qwen_timeout_ms,
        claude_api_key=settings.agent_claude_api_key,
        claude_model=settings.agent_claude_model,
        claude_timeout_ms=settings.agent_claude_timeout_ms,
        max_override_rate=settings.agent_max_override_rate,
        prompts_dir=settings.agent_prompts_dir,
        message_db_path=settings.agent_db_path,
        message_ttl_seconds=settings.agent_message_ttl_seconds,
        operator_db_path=settings.sqlite_path,
    )

    orch = AgentOrchestrator(config)
    if config.enabled:
        logger.info(
            "Agent orchestrator configured: slots=%d qwen=%s claude=%s",
            config.num_slots,
            config.qwen_url,
            config.claude_model,
        )
    else:
        logger.info("Agent orchestrator DISABLED (AGENT_ENABLED=false)")

    return orch
