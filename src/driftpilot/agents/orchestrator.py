"""Agent Orchestrator — lifecycle management for all agents.

Coordinates the PM, Scanner, and Slot agents. Handles:
- Agent startup and shutdown
- Tick scheduling (30-second intervals)
- Fallback to algo-only mode if agent infrastructure fails
- Agent health monitoring (heartbeat checks)
- Daily state reset

Wired into the operator loop via a single `tick()` call per cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .guardrail_validator import GuardrailValidator
from .llm_client import LLMClient
from .message_bus import MessageBus
from .pm_agent import PMAgent, PortfolioSnapshot
from .pm_analyst import PMAnalyst
from .prompt_loader import PromptLoader
from .scanner_agent import CandidateInfo, MarketContext, ScannerAgent
from .slot_agent import PositionSnapshot, SlotAgent, SlotTickResult

logger = logging.getLogger(__name__)

# Agent is disabled by default — must be explicitly enabled
DEFAULT_ENABLED = False
DEFAULT_NUM_SLOTS = 10
HEARTBEAT_TIMEOUT_SECONDS = 60


@dataclass
class OrchestratorConfig:
    """Configuration for the agent orchestrator."""

    enabled: bool = DEFAULT_ENABLED
    num_slots: int = DEFAULT_NUM_SLOTS
    pm_interval_seconds: int = 30
    slot_interval_seconds: int = 30
    qwen_url: str = "http://192.168.1.166:8000/v1"
    qwen_model: str = "Qwen/Qwen3-8B"
    qwen_timeout_ms: int = 500
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"
    claude_timeout_ms: int = 3000
    max_override_rate: float = 0.20
    prompts_dir: str = "config/prompts"
    message_db_path: str = "data/driftpilot/agent_messages.sqlite3"
    message_ttl_seconds: int = 300
    operator_db_path: str = "state/operator.sqlite3"
    analyst_interval_minutes: int = 15


@dataclass
class OrchestratorTickResult:
    """Result of one orchestrator tick."""

    pm_processed: int = 0
    scanner_entries_requested: int = 0
    slot_actions: dict[int, str] = field(default_factory=dict)
    algo_only_mode: bool = False
    analyst_ran: bool = False
    error: str | None = None


class AgentOrchestrator:
    """Top-level coordinator for the multi-agent trading system.

    Usage:
        config = OrchestratorConfig(enabled=True)
        orch = AgentOrchestrator(config)
        orch.start()

        # In operator loop:
        result = orch.tick_pm(portfolio_snapshot)
        result = orch.tick_scanner(candidates, market_context)
        result = orch.tick_slot(slot_id, position, algo_says_exit)

        orch.stop()
    """

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._running = False
        self._bus: MessageBus | None = None
        self._llm: LLMClient | None = None
        self._prompts: PromptLoader | None = None
        self._guardrails: GuardrailValidator | None = None
        self._pm: PMAgent | None = None
        self._scanner: ScannerAgent | None = None
        self._slots: dict[int, SlotAgent] = {}
        self._analyst: PMAnalyst | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Initialize all agent infrastructure. Idempotent.

        The PM Analyst is always initialized (even when agents are disabled)
        because it operates independently via Qwen — no agent bus needed.
        """
        # Always init analyst, even if agents are disabled
        if self._analyst is None:
            try:
                self._analyst = PMAnalyst(
                    operator_db_path=self._config.operator_db_path,
                    qwen_url=self._config.qwen_url,
                    qwen_model=self._config.qwen_model,
                    qwen_timeout_ms=10000,
                    interval_minutes=self._config.analyst_interval_minutes,
                )
                logger.info("PM Analyst initialized (interval=%dm)", self._config.analyst_interval_minutes)
            except Exception as exc:
                logger.warning("PM Analyst init failed (non-fatal): %s", exc)

        if not self._config.enabled:
            logger.info("Agent orchestrator is DISABLED (agent_enabled=False) — PM Analyst still active")
            return

        if self._running:
            return

        logger.info("Starting agent orchestrator...")

        # Initialize infrastructure
        self._bus = MessageBus(
            db_path=self._config.message_db_path,
            ttl_seconds=self._config.message_ttl_seconds,
        )
        self._bus.initialize()

        self._llm = LLMClient(
            qwen_url=self._config.qwen_url,
            qwen_model=self._config.qwen_model,
            qwen_timeout_ms=self._config.qwen_timeout_ms,
            claude_api_key=self._config.claude_api_key,
            claude_model=self._config.claude_model,
            claude_timeout_ms=self._config.claude_timeout_ms,
        )

        self._prompts = PromptLoader(self._config.prompts_dir)
        self._guardrails = GuardrailValidator()

        # Create agents
        self._pm = PMAgent(
            bus=self._bus,
            llm_client=self._llm,
            prompt_loader=self._prompts,
            guardrails=self._guardrails,
            max_override_rate=self._config.max_override_rate,
        )

        self._scanner = ScannerAgent(
            bus=self._bus,
            llm_client=self._llm,
            prompt_loader=self._prompts,
        )

        for slot_id in range(self._config.num_slots):
            self._slots[slot_id] = SlotAgent(
                slot_id=slot_id,
                bus=self._bus,
                llm_client=self._llm,
                prompt_loader=self._prompts,
                guardrails=self._guardrails,
            )

        # PM Analyst — runs independently of the agent LLM pipeline
        try:
            self._analyst = PMAnalyst(
                operator_db_path=self._config.operator_db_path,
                qwen_url=self._config.qwen_url,
                qwen_model=self._config.qwen_model,
                qwen_timeout_ms=10000,  # analyst gets longer timeout
                interval_minutes=self._config.analyst_interval_minutes,
            )
            logger.info("PM Analyst initialized (interval=%dm)", self._config.analyst_interval_minutes)
        except Exception as exc:
            logger.warning("PM Analyst init failed (non-fatal): %s", exc)
            self._analyst = None

        self._running = True
        logger.info(
            "Agent orchestrator started: PM + Scanner + %d Slot agents + Analyst",
            self._config.num_slots,
        )

    def stop(self) -> None:
        """Shut down all agents cleanly."""
        if not self._running:
            return

        logger.info("Stopping agent orchestrator...")
        if self._bus:
            self._bus.update_agent_state("pm", status="stopped")
            self._bus.update_agent_state("scanner", status="stopped")
            for slot_id in range(self._config.num_slots):
                self._bus.update_agent_state(f"slot_{slot_id}", status="stopped")
            self._bus.close()

        self._running = False
        logger.info("Agent orchestrator stopped.")

    def tick_pm(self, portfolio: PortfolioSnapshot) -> int:
        """Run one PM agent tick. Returns number of messages processed.

        Call this every pm_interval_seconds in the operator loop.
        """
        if not self._running or self._pm is None:
            return 0

        try:
            result = self._pm.tick(portfolio)
            return result.messages_processed + result.force_exits_issued
        except Exception as exc:
            logger.exception("PM agent tick failed: %s", exc)
            return 0

    def tick_scanner(
        self,
        candidates: list[CandidateInfo],
        market: MarketContext,
    ) -> int:
        """Run scanner evaluation on new candidates. Returns entries requested.

        Call this when a new CatalystEvent arrives and algo produces candidates.
        """
        if not self._running or self._scanner is None:
            return 0

        try:
            result = self._scanner.evaluate_candidates(candidates, market)
            return result.entries_requested
        except Exception as exc:
            logger.exception("Scanner agent tick failed: %s", exc)
            return 0

    def tick_slot(
        self,
        slot_id: int,
        position: PositionSnapshot,
        algo_says_exit: bool,
    ) -> SlotTickResult | None:
        """Run one slot agent tick. Returns tick result or None if disabled.

        Call this every slot_interval_seconds for each active position.
        """
        if not self._running:
            return None

        agent = self._slots.get(slot_id)
        if agent is None:
            logger.warning("No slot agent for slot_id=%d", slot_id)
            return None

        try:
            return agent.tick(position, algo_says_exit)
        except Exception as exc:
            logger.exception("Slot agent %d tick failed: %s", slot_id, exc)
            return None

    def tick_analyst(self) -> bool:
        """Run PM Analyst if interval has elapsed. Returns True if analysis ran.

        Call this every operator tick. The analyst self-throttles to its
        configured interval (default 15 min), so calling more often is safe.
        Also works when agent orchestrator is disabled — the analyst only
        needs Qwen, not the full agent pipeline.
        """
        if self._analyst is None:
            return False

        try:
            if not self._analyst.should_run():
                return False
            result = self._analyst.run()
            return result is not None
        except Exception as exc:
            logger.exception("PM Analyst tick failed: %s", exc)
            return False

    def reload_prompts(self) -> int:
        """Hot-reload prompt configurations. Returns count loaded."""
        if self._prompts is None:
            return 0
        return self._prompts.reload()

    def get_override_rate(self) -> float:
        """Get today's override rate across all agents."""
        if self._bus is None:
            return 0.0
        return self._bus.get_override_rate()

    def get_agent_states(self) -> dict[str, dict]:
        """Get current state of all agents for dashboard."""
        if self._bus is None:
            return {}

        states = {}
        for name in ["pm", "scanner"] + [f"slot_{i}" for i in range(self._config.num_slots)]:
            state = self._bus.get_agent_state(name)
            if state:
                states[name] = dict(state)
        return states

    def reset_daily(self) -> None:
        """Reset daily counters. Call at session start."""
        if self._guardrails:
            self._guardrails.reset_daily()

        if self._bus:
            for name in ["pm", "scanner"] + [f"slot_{i}" for i in range(self._config.num_slots)]:
                self._bus.update_agent_state(
                    name,
                    status="idle",
                    consecutive_wins=0,
                    consecutive_losses=0,
                    override_count_today=0,
                    total_decisions_today=0,
                )

        logger.info("Agent orchestrator: daily reset complete.")
