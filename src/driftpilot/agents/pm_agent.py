"""PM Agent — Portfolio Manager for the multi-agent trading system.

Responsibilities:
1. Process ENTRY_REQUEST from Scanner → approve/deny
2. Process TARGET_RAISE_REQUEST from Slot Agents → approve/deny
3. Process PARTIAL_PROFIT_REQUEST from Slot Agents → approve/deny
4. Process EARLY_CUT_REQUEST from Slot Agents → approve/deny
5. Issue FORCE_EXIT when portfolio-level risk dictates
6. Adapt session parameters after patterns emerge

Fallback if LLM down: Approve entries that pass algo + allocator checks.
Deny target raises. No session adaptation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from .brain_client import (
    BrainClient,
    BrainQueryResult,
    format_experiences_for_prompt,
    format_skills_for_prompt,
)
from .guardrail_validator import GuardrailValidator, PortfolioState
from .llm_client import LLMClient
from .message_bus import MessageBus
from .models import AgentMessage, MessageType
from .prompt_loader import PromptConfig, PromptLoader

logger = logging.getLogger(__name__)


@dataclass
class PortfolioSnapshot:
    """Current portfolio state for PM decision-making."""

    open_slots: int
    total_slots: int
    sector_exposure: dict[str, int]
    daily_pnl_pct: float
    consecutive_wins: int
    consecutive_losses: int
    minutes_left_in_session: int
    last_trade_result: str  # "win" | "loss" | "none"
    override_count_today: int
    total_decisions_today: int


@dataclass
class PMTickResult:
    """Result of one PM agent tick."""

    messages_processed: int
    entries_approved: int
    entries_denied: int
    raises_approved: int
    raises_denied: int
    cuts_approved: int
    force_exits_issued: int
    session_adapted: bool


class PMAgent:
    """Portfolio Manager Agent — approves/denies requests from other agents.

    Deterministic checks run FIRST (sector cap, daily loss, slot availability).
    LLM is only consulted AFTER deterministic checks pass.
    """

    def __init__(
        self,
        bus: MessageBus,
        llm_client: LLMClient,
        prompt_loader: PromptLoader,
        guardrails: GuardrailValidator,
        max_override_rate: float = 0.20,
        brain_client: BrainClient | None = None,
    ) -> None:
        self.agent_name = "pm"
        self._bus = bus
        self._llm = llm_client
        self._prompts = prompt_loader
        self._guardrails = guardrails
        self._max_override_rate = max_override_rate
        self._brain = brain_client if brain_client is not None else BrainClient()
        self._brain_experience_by_symbol: dict[str, str] = {}

    def tick(self, portfolio: PortfolioSnapshot) -> PMTickResult:
        """Process all pending messages for one PM cycle.

        Args:
            portfolio: Current portfolio state snapshot.

        Returns:
            PMTickResult with counts of actions taken.
        """
        self._bus.update_agent_state(self.agent_name, status="running")

        result = PMTickResult(
            messages_processed=0,
            entries_approved=0,
            entries_denied=0,
            raises_approved=0,
            raises_denied=0,
            cuts_approved=0,
            force_exits_issued=0,
            session_adapted=False,
        )

        # Check if we should force-exit everything (daily loss limit)
        if self._guardrails.should_force_exit_all(portfolio.daily_pnl_pct):
            result.force_exits_issued = self._force_exit_all(portfolio, "daily_loss_limit")
            return result

        # Process entry requests
        entry_requests = self._bus.poll(
            "pm", msg_types=[MessageType.ENTRY_REQUEST]
        )
        for msg in entry_requests:
            self._bus.ack(msg.msg_id)
            approved = self._process_entry_request(msg, portfolio)
            if approved:
                result.entries_approved += 1
            else:
                result.entries_denied += 1
            result.messages_processed += 1

        # Process target raise requests
        raise_requests = self._bus.poll(
            "pm", msg_types=[MessageType.TARGET_RAISE_REQUEST]
        )
        for msg in raise_requests:
            self._bus.ack(msg.msg_id)
            approved = self._process_target_raise(msg, portfolio)
            if approved:
                result.raises_approved += 1
            else:
                result.raises_denied += 1
            result.messages_processed += 1

        # Process partial profit requests
        partial_requests = self._bus.poll(
            "pm", msg_types=[MessageType.PARTIAL_PROFIT_REQUEST]
        )
        for msg in partial_requests:
            self._bus.ack(msg.msg_id)
            approved = self._process_partial_profit(msg, portfolio)
            if approved:
                result.cuts_approved += 1
            result.messages_processed += 1

        # Process early cut requests
        cut_requests = self._bus.poll(
            "pm", msg_types=[MessageType.EARLY_CUT_REQUEST]
        )
        for msg in cut_requests:
            self._bus.ack(msg.msg_id)
            approved = self._process_early_cut(msg, portfolio)
            if approved:
                result.cuts_approved += 1
            result.messages_processed += 1

        # Process exit reports and best-effort brain outcome persistence.
        exit_reports = self._bus.poll(
            "pm", msg_types=[MessageType.EXIT_REPORT]
        )
        for msg in exit_reports:
            self._bus.ack(msg.msg_id)
            self._process_exit_report(msg)
            self._bus.mark_processed(msg.msg_id)
            result.messages_processed += 1

        # Update state
        self._bus.update_agent_state(
            self.agent_name,
            status="running",
            consecutive_wins=portfolio.consecutive_wins,
            consecutive_losses=portfolio.consecutive_losses,
            override_count_today=portfolio.override_count_today,
            total_decisions_today=portfolio.total_decisions_today,
        )

        return result

    def _process_entry_request(
        self, msg: AgentMessage, portfolio: PortfolioSnapshot
    ) -> bool:
        """Process an ENTRY_REQUEST. Returns True if approved."""
        payload = msg.payload
        symbol = payload.get("symbol", "")
        sector = payload.get("sector", "unknown")
        target_pct = float(payload.get("proposed_target_pct", 0.01))
        stop_pct = float(payload.get("proposed_stop_pct", 0.015))

        # --- Deterministic checks FIRST ---
        portfolio_state = PortfolioState(
            free_slots=portfolio.total_slots - portfolio.open_slots,
            sector_counts=portfolio.sector_exposure,
            daily_pnl_pct=portfolio.daily_pnl_pct,
            total_positions=portfolio.open_slots,
        )

        # Check override rate
        if self._guardrails.is_override_rate_exceeded(
            portfolio.override_count_today,
            portfolio.total_decisions_today,
            self._max_override_rate,
        ):
            self._send_decision(msg, "deny", "override_rate_exceeded", target_pct, 1.0)
            return False

        # Last 30 minutes check
        if portfolio.minutes_left_in_session < 30:
            self._send_decision(msg, "deny", "session_ending_<30min", target_pct, 1.0)
            return False

        # Guardrail validation
        validated = self._guardrails.validate_entry(
            symbol=symbol,
            target_pct=target_pct,
            stop_pct=stop_pct,
            size_multiplier=1.0,
            sector=sector,
            portfolio=portfolio_state,
        )
        if not validated.allowed:
            self._send_decision(
                msg, "deny", f"guardrail: {validated.denial_reason}", target_pct, 1.0
            )
            return False

        # --- LLM evaluation (deterministic checks passed) ---
        prompt = self._prompts.get("pm_entry_approval")
        brain_context = self._build_entry_brain_context(payload, portfolio)
        brain_result = self._query_brain(symbol, "entry_decision", brain_context)
        brain_prompt_context = self._format_brain_prompt_context(brain_result)
        prompt = self._with_brain_prompt_context(prompt, brain_prompt_context)

        template_vars = {
            "symbol": symbol,
            "signal_name": payload.get("signal_name", ""),
            "algo_score": payload.get("algo_score", 0),
            "headline": payload.get("headline", ""),
            "sentiment": payload.get("sentiment", ""),
            "confidence": payload.get("confidence", 0),
            "priority_mod": payload.get("priority_modifier", 0),
            "target_pct": f"{target_pct * 100:.2f}",
            "stop_pct": f"{stop_pct * 100:.2f}",
            "open_slots": portfolio.open_slots,
            "sector_exposure": str(portfolio.sector_exposure),
            "daily_pnl_pct": f"{portfolio.daily_pnl_pct * 100:.2f}",
            "consec_losses": portfolio.consecutive_losses,
            "consec_wins": portfolio.consecutive_wins,
            "minutes_left": portfolio.minutes_left_in_session,
            "last_trade_result": portfolio.last_trade_result,
        }
        if brain_prompt_context:
            template_vars["brain_context"] = brain_prompt_context

        llm_response = self._llm.complete(prompt, template_vars)
        decision = llm_response.parsed.get("decision", "approve")
        reasoning = llm_response.parsed.get("reasoning", "no reasoning")
        llm_target = float(llm_response.parsed.get("target_pct", target_pct))
        llm_size = float(llm_response.parsed.get("size_multiplier", 1.0))

        # Re-validate LLM's suggested values through guardrails
        re_validated = self._guardrails.validate_entry(
            symbol=symbol,
            target_pct=llm_target,
            stop_pct=stop_pct,
            size_multiplier=llm_size,
            sector=sector,
            portfolio=portfolio_state,
        )

        # Log decision
        self._bus.log_decision(
            agent_name=self.agent_name,
            decision_type="entry_approval",
            algo_recommendation="approve",  # Scanner already approved via algo
            agent_decision=decision,
            reasoning=reasoning,
            llm_model=llm_response.model,
            llm_latency_ms=llm_response.latency_ms,
            prompt_version=prompt.version,
            inputs_json=template_vars,
            raw_response=llm_response.raw,
            symbol=symbol,
            is_override=(decision == "deny"),
            confidence=llm_response.parsed.get("confidence"),
        )

        approved = decision == "approve"
        brain_experience_id = (
            self._store_entry_experience(
                context=brain_context,
                decision={
                    "action": decision,
                    "reasoning": reasoning,
                    "target_pct": re_validated.target_pct if approved else target_pct,
                    "size_multiplier": (
                        re_validated.size_multiplier if approved else 1.0
                    ),
                },
                metadata={
                    "message_id": msg.msg_id,
                    "correlation_id": msg.correlation_id,
                    "llm_model": llm_response.model,
                    "prompt_version": prompt.version,
                },
            )
            if not brain_result.is_fallback
            else None
        )
        if brain_experience_id and symbol:
            self._brain_experience_by_symbol[symbol] = brain_experience_id

        self._send_decision(
            msg,
            decision,
            reasoning,
            re_validated.target_pct if approved else target_pct,
            re_validated.size_multiplier if approved else 1.0,
            brain_experience_id=brain_experience_id,
        )
        self._bus.mark_processed(msg.msg_id)
        return approved

    def _process_target_raise(
        self, msg: AgentMessage, portfolio: PortfolioSnapshot
    ) -> bool:
        """Process TARGET_RAISE_REQUEST. Returns True if approved."""
        payload = msg.payload
        symbol = payload.get("symbol", "")
        proposed = float(payload.get("proposed_target_pct", 0.02))
        confidence = float(payload.get("confidence", 0))
        self._query_brain(
            symbol,
            "target_raise_decision",
            self._build_exit_like_brain_context(payload, portfolio, "target_raise"),
        )

        # Validate through guardrails
        validated = self._guardrails.validate_target_raise(
            symbol=symbol,
            new_target_pct=proposed,
            new_stop_pct=0.0,  # trailing stop stays where it is
        )

        # Simple heuristic: approve if confidence > 0.7 and not in drawdown
        approved = confidence >= 0.7 and portfolio.daily_pnl_pct > -0.02
        decision = "approve" if approved else "deny"
        reasoning = (
            f"confidence={confidence:.2f}, daily_pnl={portfolio.daily_pnl_pct:.3f}"
        )

        response = AgentMessage(
            msg_type=MessageType.TARGET_RAISE_DECISION,
            from_agent=self.agent_name,
            to_agent=msg.from_agent,
            correlation_id=msg.correlation_id or msg.msg_id,
            payload={
                "decision": decision,
                "approved_target_pct": validated.new_target_pct if approved else None,
                "reasoning": reasoning,
            },
        )
        self._bus.send(response)
        self._bus.mark_processed(msg.msg_id)

        logger.info(
            "pm: TARGET_RAISE %s for %s (%.4f → %.4f, conf=%.2f)",
            decision,
            symbol,
            float(payload.get("current_target_pct", 0)),
            validated.new_target_pct,
            confidence,
        )
        return approved

    def _process_partial_profit(
        self, msg: AgentMessage, portfolio: PortfolioSnapshot
    ) -> bool:
        """Process PARTIAL_PROFIT_REQUEST. Returns True if approved."""
        payload = msg.payload
        symbol = payload.get("symbol", "")
        unrealized = float(payload.get("unrealized_pct", 0))
        hold_min = int(payload.get("hold_minutes", 0))
        confidence = float(payload.get("confidence", 0))
        self._query_brain(
            symbol,
            "partial_profit_decision",
            self._build_exit_like_brain_context(payload, portfolio, "partial_profit"),
        )

        # Approve if: held > 20 min, unrealized > 0.5%, confidence > 0.6
        approved = hold_min >= 20 and unrealized > 0.5 and confidence >= 0.6
        decision = "approve" if approved else "deny"
        reasoning = f"held={hold_min}min, unrealized={unrealized:.2f}%, conf={confidence:.2f}"

        response = AgentMessage(
            msg_type=MessageType.PARTIAL_PROFIT_DECISION,
            from_agent=self.agent_name,
            to_agent=msg.from_agent,
            correlation_id=msg.correlation_id or msg.msg_id,
            payload={"decision": decision, "reasoning": reasoning},
        )
        self._bus.send(response)
        self._bus.mark_processed(msg.msg_id)
        return approved

    def _process_early_cut(
        self, msg: AgentMessage, portfolio: PortfolioSnapshot
    ) -> bool:
        """Process EARLY_CUT_REQUEST. Returns True if approved."""
        payload = msg.payload
        symbol = payload.get("symbol", "")
        confidence = float(payload.get("confidence", 0))
        self._query_brain(
            symbol,
            "early_cut_decision",
            self._build_exit_like_brain_context(payload, portfolio, "early_cut"),
        )

        # Only approve high-conviction cuts
        approved = confidence >= 0.75
        decision = "approve" if approved else "deny"
        reasoning = f"confidence={confidence:.2f}, threshold=0.75"

        response = AgentMessage(
            msg_type=MessageType.EARLY_CUT_DECISION,
            from_agent=self.agent_name,
            to_agent=msg.from_agent,
            correlation_id=msg.correlation_id or msg.msg_id,
            payload={"decision": decision, "reasoning": reasoning},
        )
        self._bus.send(response)
        self._bus.mark_processed(msg.msg_id)
        return approved

    def _force_exit_all(self, portfolio: PortfolioSnapshot, reason: str) -> int:
        """Send FORCE_EXIT to all active slot agents."""
        count = 0
        for slot_id in range(portfolio.total_slots):
            msg = AgentMessage(
                msg_type=MessageType.FORCE_EXIT,
                from_agent=self.agent_name,
                to_agent=f"slot_{slot_id}",
                payload={"slot_id": slot_id, "reason": reason, "symbol": ""},
            )
            self._bus.send(msg)
            count += 1
        logger.critical(
            "pm: FORCE_EXIT ALL — reason=%s, daily_pnl=%.3f%%",
            reason,
            portfolio.daily_pnl_pct * 100,
        )
        return count

    def _send_decision(
        self,
        original_msg: AgentMessage,
        decision: str,
        reasoning: str,
        target_pct: float,
        size_multiplier: float,
        brain_experience_id: str | None = None,
    ) -> None:
        """Send ENTRY_DECISION back to scanner."""
        payload: dict[str, Any] = {
            "decision": decision,
            "reasoning": reasoning,
            "target_pct": target_pct,
            "size_multiplier": size_multiplier,
        }
        if brain_experience_id:
            payload["brain_experience_id"] = brain_experience_id

        response = AgentMessage(
            msg_type=MessageType.ENTRY_DECISION,
            from_agent=self.agent_name,
            to_agent=original_msg.from_agent,
            correlation_id=original_msg.correlation_id or original_msg.msg_id,
            payload=payload,
        )
        self._bus.send(response)

    def _query_brain(
        self,
        symbol: str,
        decision_type: str,
        context: dict[str, Any],
    ) -> BrainQueryResult:
        """Fetch similar experiences for a decision without making brain required."""
        query_context = {
            **context,
            "symbol": symbol,
            "decision_type": decision_type,
        }
        try:
            return self._brain.query(query_context)
        except Exception as exc:
            logger.warning(
                "brain_query_failed",
                extra={"symbol": symbol, "decision_type": decision_type, "error": str(exc)},
            )
            return BrainQueryResult(is_fallback=True)

    def _format_brain_prompt_context(self, result: BrainQueryResult) -> str:
        """Format brain results for prompt injection when the brain is online."""
        if result.is_fallback:
            return ""

        blocks = [
            block
            for block in (
                format_experiences_for_prompt(result.experiences),
                format_skills_for_prompt(result.skills),
            )
            if block
        ]
        return "\n\n".join(blocks)

    def _with_brain_prompt_context(
        self, prompt: PromptConfig, brain_context: str
    ) -> PromptConfig:
        """Append brain context to a prompt only when non-fallback context exists."""
        if not brain_context:
            return prompt
        return replace(
            prompt,
            user_template=(
                f"{prompt.user_template.rstrip()}\n\n"
                "Past similar trades and learned skills:\n{brain_context}\n"
            ),
        )

    def _build_entry_brain_context(
        self, payload: dict[str, Any], portfolio: PortfolioSnapshot
    ) -> dict[str, Any]:
        """Build structured context for entry RAG and experience storage."""
        symbol = str(payload.get("symbol", ""))
        signal = str(payload.get("signal_name", ""))
        headline = str(payload.get("headline", ""))
        context_text = (
            f"{symbol} {signal}: {headline}; "
            f"sentiment={payload.get('sentiment', '')}; "
            f"confidence={payload.get('confidence', 0)}; "
            f"algo_score={payload.get('algo_score', 0)}; "
            f"daily_pnl={portfolio.daily_pnl_pct:.4f}; "
            f"open_slots={portfolio.open_slots}/{portfolio.total_slots}"
        )
        return {
            "symbol": symbol,
            "signal": signal,
            "headline": headline,
            "sentiment": payload.get("sentiment", ""),
            "confidence": float(payload.get("confidence", 0)),
            "algo_score": float(payload.get("algo_score", 0)),
            "priority_modifier": float(payload.get("priority_modifier", 0)),
            "target_pct": float(payload.get("proposed_target_pct", 0.01)),
            "stop_pct": float(payload.get("proposed_stop_pct", 0.015)),
            "sector": payload.get("sector", "unknown"),
            "daily_pnl_pct": portfolio.daily_pnl_pct * 100,
            "open_slots": portfolio.open_slots,
            "total_slots": portfolio.total_slots,
            "sector_exposure": portfolio.sector_exposure,
            "consecutive_wins": portfolio.consecutive_wins,
            "consecutive_losses": portfolio.consecutive_losses,
            "minutes_left_in_session": portfolio.minutes_left_in_session,
            "minutes_in_session": max(0, 390 - portfolio.minutes_left_in_session),
            "last_trade_result": portfolio.last_trade_result,
            "context_text": context_text,
        }

    def _build_exit_like_brain_context(
        self,
        payload: dict[str, Any],
        portfolio: PortfolioSnapshot,
        action: str,
    ) -> dict[str, Any]:
        """Build structured context for PM decisions on slot-agent exit requests."""
        symbol = str(payload.get("symbol", ""))
        context_text = (
            f"{symbol} {action}: unrealized={payload.get('unrealized_pct', 0)}; "
            f"confidence={payload.get('confidence', 0)}; "
            f"reasoning={payload.get('reasoning', '')}; "
            f"daily_pnl={portfolio.daily_pnl_pct:.4f}"
        )
        return {
            "symbol": symbol,
            "signal": action,
            "confidence": float(payload.get("confidence", 0)),
            "unrealized_pct": float(payload.get("unrealized_pct", 0)),
            "hold_minutes": int(payload.get("hold_minutes", 0)),
            "daily_pnl_pct": portfolio.daily_pnl_pct * 100,
            "open_slots": portfolio.open_slots,
            "consecutive_wins": portfolio.consecutive_wins,
            "consecutive_losses": portfolio.consecutive_losses,
            "minutes_left_in_session": portfolio.minutes_left_in_session,
            "minutes_in_session": max(0, 390 - portfolio.minutes_left_in_session),
            "context_text": context_text,
        }

    def _store_entry_experience(
        self,
        context: dict[str, Any],
        decision: dict[str, Any],
        metadata: dict[str, Any],
    ) -> str | None:
        """Store the pending entry decision for later outcome backfill."""
        try:
            return self._brain.store(
                context=context,
                decision=decision,
                exp_type="entry_decision",
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning(
                "brain_store_failed",
                extra={"symbol": context.get("symbol"), "error": str(exc)},
            )
            return None

    def _process_exit_report(self, msg: AgentMessage) -> None:
        """Record or backfill the final outcome for a closed trade."""
        payload = msg.payload
        symbol = str(payload.get("symbol", ""))
        outcome = {
            "symbol": symbol,
            "exit_reason": payload.get("exit_reason", "unknown"),
            "pnl_pct": float(payload.get("pnl_pct", 0)),
            "hold_minutes": int(payload.get("hold_minutes", 0)),
            "was_override": bool(payload.get("was_override", False)),
            "slot_id": payload.get("slot_id"),
            "message_id": msg.msg_id,
            "correlation_id": msg.correlation_id,
        }
        experience_id = self._experience_id_from_exit_payload(payload)
        if experience_id is None and symbol:
            experience_id = self._brain_experience_by_symbol.pop(symbol, None)

        try:
            if experience_id:
                self._brain.backfill(experience_id, outcome)
            else:
                self._brain.store(
                    context={
                        "symbol": symbol,
                        "signal": "exit_report",
                        "context_text": (
                            f"{symbol} exit: pnl={outcome['pnl_pct']}; "
                            f"reason={outcome['exit_reason']}; "
                            f"hold_minutes={outcome['hold_minutes']}"
                        ),
                    },
                    decision={
                        "action": "exit",
                        "reasoning": str(payload.get("exit_reason", "unknown")),
                    },
                    exp_type="exit_report",
                    outcome=outcome,
                    metadata={"message_id": msg.msg_id, "correlation_id": msg.correlation_id},
                )
        except Exception as exc:
            logger.warning(
                "brain_outcome_store_failed",
                extra={"symbol": symbol, "experience_id": experience_id, "error": str(exc)},
            )

    def _experience_id_from_exit_payload(self, payload: dict[str, Any]) -> str | None:
        for key in (
            "brain_experience_id",
            "entry_brain_experience_id",
            "experience_id",
        ):
            value = payload.get(key)
            if value:
                return str(value)
        return None
