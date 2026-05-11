"""Slot Agent — manages one open position per slot.

Each slot agent runs every 30 seconds while its slot has an open position.
Flow:
1. signal.evaluate_exit() → if EXIT: execute immediately (no LLM)
2. If HOLD: LLM evaluates tape → HOLD | REQUEST_TARGET_RAISE | REQUEST_PARTIAL | REQUEST_CUT
3. Slot agent cannot act without PM approval (except algo-triggered exits)

Fallback if LLM down: follow algo exactly (HOLD when algo says HOLD).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .guardrail_validator import GuardrailValidator
from .llm_client import LLMClient, LLMResponse
from .message_bus import MessageBus
from .models import AgentMessage, MessageType
from .prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


@dataclass
class PositionSnapshot:
    """Current state of a position for slot agent evaluation."""

    symbol: str
    slot_id: int
    entry_price: float
    current_price: float
    unrealized_pct: float
    target_pct: float
    stop_pct: float
    hold_minutes: int
    max_hold_minutes: int
    last_10_closes: list[float]
    last_10_volumes: list[int]
    high_pct: float  # high since entry as %
    low_pct: float  # low since entry as %
    consolidation_bars: int
    recent_vol: int
    avg_vol: int
    rvol: float  # relative volume ratio
    sector_move_pct: float
    spy_move_pct: float
    vix: float
    new_headlines: str  # text block of new headlines since entry
    signal_name: str


@dataclass
class SlotTickResult:
    """Result of a single slot agent tick."""

    action: str  # "hold" | "algo_exit" | "request_target_raise" | "request_partial_profit" | "request_early_cut"
    reasoning: str
    confidence: float
    llm_latency_ms: int
    used_fallback: bool
    message_sent: AgentMessage | None = None


class SlotAgent:
    """Manages one trading slot — evaluates position every tick.

    The algo (signal.evaluate_exit) runs FIRST and is authoritative.
    The LLM is ONLY consulted when algo says HOLD.
    """

    def __init__(
        self,
        slot_id: int,
        bus: MessageBus,
        llm_client: LLMClient,
        prompt_loader: PromptLoader,
        guardrails: GuardrailValidator,
    ) -> None:
        self.slot_id = slot_id
        self.agent_name = f"slot_{slot_id}"
        self._bus = bus
        self._llm = llm_client
        self._prompts = prompt_loader
        self._guardrails = guardrails
        self._pending_request_id: str | None = None

    def tick(
        self,
        position: PositionSnapshot,
        algo_says_exit: bool,
    ) -> SlotTickResult:
        """Run one evaluation cycle for this slot's position.

        Args:
            position: Current state of the position in this slot.
            algo_says_exit: Whether signal.evaluate_exit() returned should_exit=True.

        Returns:
            SlotTickResult with the action taken.
        """
        # Update heartbeat
        self._bus.update_agent_state(self.agent_name, status="running")

        # --- ALGO EXIT IS AUTHORITATIVE ---
        if algo_says_exit:
            logger.info(
                "slot_%d: ALGO EXIT for %s (no LLM involved)",
                self.slot_id,
                position.symbol,
            )
            return SlotTickResult(
                action="algo_exit",
                reasoning="signal.evaluate_exit() returned should_exit=True",
                confidence=1.0,
                llm_latency_ms=0,
                used_fallback=False,
            )

        # --- Check for PM responses to our pending requests ---
        if self._pending_request_id:
            response = self._bus.get_response(self._pending_request_id)
            if response and response.status.value == "processed":
                # PM has responded — handle it
                self._pending_request_id = None
                # The orchestrator will handle the actual execution

        # --- LLM evaluation (only when algo says HOLD) ---
        prompt = self._prompts.get("slot_exit_override")
        template_vars = self._build_template_vars(position)

        llm_response = self._llm.complete(prompt, template_vars)

        action = self._extract_action(llm_response)
        confidence = self._extract_confidence(llm_response)
        reasoning = llm_response.parsed.get("reasoning", "no reasoning")

        # Log the decision
        is_override = action != "hold"
        self._bus.log_decision(
            agent_name=self.agent_name,
            decision_type="exit_override",
            algo_recommendation="hold",
            agent_decision=action,
            reasoning=reasoning,
            llm_model=llm_response.model,
            llm_latency_ms=llm_response.latency_ms,
            prompt_version=prompt.version,
            inputs_json=template_vars,
            raw_response=llm_response.raw,
            symbol=position.symbol,
            slot_id=self.slot_id,
            is_override=is_override,
            confidence=confidence,
        )

        # --- Send message to PM if override requested ---
        message_sent: AgentMessage | None = None
        if action == "request_target_raise":
            message_sent = self._send_target_raise_request(position, llm_response)
        elif action == "request_partial_profit":
            message_sent = self._send_partial_profit_request(position, llm_response)
        elif action == "request_early_cut":
            # Validate minimum hold time before sending cut request
            hold_seconds = position.hold_minutes * 60
            exit_check = self._guardrails.validate_exit(
                symbol=position.symbol,
                slot_id=self.slot_id,
                hold_seconds=hold_seconds,
                is_algo_exit=False,
            )
            if exit_check.allowed:
                message_sent = self._send_early_cut_request(position, llm_response)
            else:
                logger.info(
                    "slot_%d: early cut blocked by guardrail: %s",
                    self.slot_id,
                    exit_check.denial_reason,
                )
                action = "hold"
                reasoning = f"guardrail blocked: {exit_check.denial_reason}"

        return SlotTickResult(
            action=action,
            reasoning=reasoning,
            confidence=confidence,
            llm_latency_ms=llm_response.latency_ms,
            used_fallback=llm_response.used_fallback,
            message_sent=message_sent,
        )

    def _build_template_vars(self, pos: PositionSnapshot) -> dict[str, Any]:
        """Build template variables for the slot exit override prompt."""
        return {
            "symbol": pos.symbol,
            "entry_price": f"{pos.entry_price:.2f}",
            "current_price": f"{pos.current_price:.2f}",
            "unrealized_pct": f"{pos.unrealized_pct:.2f}",
            "target_pct": f"{pos.target_pct * 100:.2f}",
            "stop_pct": f"{pos.stop_pct * 100:.2f}",
            "hold_min": pos.hold_minutes,
            "max_hold_min": pos.max_hold_minutes,
            "last_10_closes": ", ".join(f"{c:.2f}" for c in pos.last_10_closes[-10:]),
            "last_10_volumes": ", ".join(str(v) for v in pos.last_10_volumes[-10:]),
            "high_pct": f"{pos.high_pct:.2f}",
            "low_pct": f"{pos.low_pct:.2f}",
            "consolidation_bars": pos.consolidation_bars,
            "recent_vol": pos.recent_vol,
            "avg_vol": pos.avg_vol,
            "rvol": f"{pos.rvol:.1f}",
            "sector_move": f"{pos.sector_move_pct:.2f}",
            "spy_move": f"{pos.spy_move_pct:.2f}",
            "vix": f"{pos.vix:.1f}",
            "new_headlines": pos.new_headlines or "(none)",
        }

    def _extract_action(self, response: LLMResponse) -> str:
        """Extract the action from LLM response, defaulting to hold."""
        action = response.parsed.get("action", "hold")
        valid_actions = {"hold", "request_target_raise", "request_partial_profit", "request_early_cut"}
        if action not in valid_actions:
            logger.warning(
                "slot_%d: invalid action '%s', defaulting to hold",
                self.slot_id,
                action,
            )
            return "hold"
        return action

    def _extract_confidence(self, response: LLMResponse) -> float:
        """Extract confidence from LLM response."""
        conf = response.parsed.get("confidence", 0.0)
        try:
            return max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            return 0.0

    def _send_target_raise_request(
        self, pos: PositionSnapshot, llm_resp: LLMResponse
    ) -> AgentMessage:
        """Send TARGET_RAISE_REQUEST to PM."""
        proposed_target = llm_resp.parsed.get("target_raise_to_pct", pos.target_pct * 1.5)
        # Validate through guardrails
        validated = self._guardrails.validate_target_raise(
            symbol=pos.symbol,
            new_target_pct=float(proposed_target),
            new_stop_pct=pos.stop_pct,
        )

        msg = AgentMessage(
            msg_type=MessageType.TARGET_RAISE_REQUEST,
            from_agent=self.agent_name,
            to_agent="pm",
            correlation_id=None,
            payload={
                "symbol": pos.symbol,
                "slot_id": self.slot_id,
                "current_target_pct": pos.target_pct,
                "proposed_target_pct": validated.new_target_pct,
                "unrealized_pct": pos.unrealized_pct,
                "reasoning": llm_resp.parsed.get("reasoning", ""),
                "confidence": self._extract_confidence(llm_resp),
            },
        )
        msg.correlation_id = msg.msg_id
        self._bus.send(msg)
        self._pending_request_id = msg.msg_id
        logger.info(
            "slot_%d: TARGET_RAISE_REQUEST for %s (%.2f%% → %.4f)",
            self.slot_id,
            pos.symbol,
            pos.unrealized_pct,
            validated.new_target_pct,
        )
        return msg

    def _send_partial_profit_request(
        self, pos: PositionSnapshot, llm_resp: LLMResponse
    ) -> AgentMessage:
        """Send PARTIAL_PROFIT_REQUEST to PM."""
        msg = AgentMessage(
            msg_type=MessageType.PARTIAL_PROFIT_REQUEST,
            from_agent=self.agent_name,
            to_agent="pm",
            payload={
                "symbol": pos.symbol,
                "slot_id": self.slot_id,
                "unrealized_pct": pos.unrealized_pct,
                "hold_minutes": pos.hold_minutes,
                "reasoning": llm_resp.parsed.get("reasoning", ""),
                "confidence": self._extract_confidence(llm_resp),
            },
        )
        msg.correlation_id = msg.msg_id
        self._bus.send(msg)
        self._pending_request_id = msg.msg_id
        logger.info(
            "slot_%d: PARTIAL_PROFIT_REQUEST for %s (%.2f%%, %d min)",
            self.slot_id,
            pos.symbol,
            pos.unrealized_pct,
            pos.hold_minutes,
        )
        return msg

    def _send_early_cut_request(
        self, pos: PositionSnapshot, llm_resp: LLMResponse
    ) -> AgentMessage:
        """Send EARLY_CUT_REQUEST to PM."""
        msg = AgentMessage(
            msg_type=MessageType.EARLY_CUT_REQUEST,
            from_agent=self.agent_name,
            to_agent="pm",
            payload={
                "symbol": pos.symbol,
                "slot_id": self.slot_id,
                "unrealized_pct": pos.unrealized_pct,
                "reasoning": llm_resp.parsed.get("reasoning", ""),
                "confidence": self._extract_confidence(llm_resp),
            },
        )
        msg.correlation_id = msg.msg_id
        self._bus.send(msg)
        self._pending_request_id = msg.msg_id
        logger.info(
            "slot_%d: EARLY_CUT_REQUEST for %s (%.2f%%)",
            self.slot_id,
            pos.symbol,
            pos.unrealized_pct,
        )
        return msg
