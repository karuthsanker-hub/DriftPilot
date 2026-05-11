"""Scanner Agent — evaluates catalyst events and pitches entries to PM.

Trigger: Each CatalystEvent from the event bus.
Flow:
1. Router.route(event) → RoutingDecision
2. If BLOCK: stop (no trade)
3. If ROUTE: signal.scan(now) → list[Candidate]
4. LLM evaluates each candidate: approve / skip / force_enter
5. For each approved: emit ENTRY_REQUEST to PM

Fallback if LLM down: Emit ENTRY_REQUEST for every candidate that passes algo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .llm_client import LLMClient, LLMResponse
from .message_bus import MessageBus
from .models import AgentMessage, MessageType
from .prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


@dataclass
class CandidateInfo:
    """A candidate from the algo pipeline for scanner evaluation."""

    symbol: str
    signal_name: str
    algo_score: float
    headline: str
    category: str
    subcategory: str
    sentiment: str
    confidence: float
    priority_modifier: float
    sector: str
    minutes_since_headline: int
    same_symbol_traded_today: bool
    similar_headlines_last_2h: int
    catalyst_event_id: int | None = None


@dataclass
class MarketContext:
    """Market context for scanner evaluation."""

    spy_change_pct: float
    vix: float
    sector_change_pct: float  # Sector ETF change for this candidate's sector


@dataclass
class ScannerTickResult:
    """Result of scanner processing one catalyst event."""

    candidates_evaluated: int
    entries_requested: int
    candidates_skipped: int
    used_fallback: bool
    messages_sent: list[AgentMessage]


class ScannerAgent:
    """Evaluates catalyst-triggered candidates and pitches them to PM.

    The algo (Router + signal.scan) runs FIRST. Scanner LLM decides
    whether to confirm, skip, or force-enter.
    """

    def __init__(
        self,
        bus: MessageBus,
        llm_client: LLMClient,
        prompt_loader: PromptLoader,
    ) -> None:
        self.agent_name = "scanner"
        self._bus = bus
        self._llm = llm_client
        self._prompts = prompt_loader

    def evaluate_candidates(
        self,
        candidates: list[CandidateInfo],
        market: MarketContext,
    ) -> ScannerTickResult:
        """Evaluate a batch of candidates from one catalyst event.

        Args:
            candidates: Candidates that passed algo screening.
            market: Current market conditions.

        Returns:
            ScannerTickResult with counts and messages sent.
        """
        self._bus.update_agent_state(self.agent_name, status="running")

        if not candidates:
            return ScannerTickResult(
                candidates_evaluated=0,
                entries_requested=0,
                candidates_skipped=0,
                used_fallback=False,
                messages_sent=[],
            )

        # Build LLM prompt with all candidates
        prompt = self._prompts.get("scanner_override")
        # Use first candidate for shared context (they come from same event)
        first = candidates[0]
        template_vars = self._build_template_vars(first, candidates, market)

        llm_response = self._llm.complete(prompt, template_vars)
        used_fallback = llm_response.used_fallback

        # Extract per-candidate decisions
        decisions = self._extract_decisions(llm_response, candidates)

        messages_sent: list[AgentMessage] = []
        entries_requested = 0
        candidates_skipped = 0

        for candidate, decision in zip(candidates, decisions):
            action = decision.get("action", "approve")

            # Log the decision
            is_override = action != "approve"
            self._bus.log_decision(
                agent_name=self.agent_name,
                decision_type="scanner_eval",
                algo_recommendation="approve",  # Algo already approved
                agent_decision=action,
                reasoning=llm_response.parsed.get("reasoning", ""),
                llm_model=llm_response.model,
                llm_latency_ms=llm_response.latency_ms,
                prompt_version=prompt.version,
                inputs_json={"symbol": candidate.symbol, "headline": candidate.headline},
                raw_response=llm_response.raw,
                symbol=candidate.symbol,
                is_override=is_override,
            )

            if action == "skip":
                candidates_skipped += 1
                logger.info(
                    "scanner: SKIP %s — %s",
                    candidate.symbol,
                    decision.get("reasoning", "no reason"),
                )
                continue

            # approve or force_enter → send ENTRY_REQUEST to PM
            target_suggestion = decision.get(
                "target_pct_suggestion", 0.01
            )

            msg = AgentMessage(
                msg_type=MessageType.ENTRY_REQUEST,
                from_agent=self.agent_name,
                to_agent="pm",
                payload={
                    "symbol": candidate.symbol,
                    "signal_name": candidate.signal_name,
                    "algo_score": candidate.algo_score,
                    "headline": candidate.headline,
                    "sentiment": candidate.sentiment,
                    "confidence": candidate.confidence,
                    "priority_modifier": candidate.priority_modifier,
                    "proposed_target_pct": float(target_suggestion),
                    "proposed_stop_pct": 0.015,
                    "sector": candidate.sector,
                    "catalyst_event_id": candidate.catalyst_event_id,
                    "is_force_enter": action == "force_enter",
                },
            )
            msg.correlation_id = msg.msg_id
            self._bus.send(msg)
            messages_sent.append(msg)
            entries_requested += 1

            logger.info(
                "scanner: ENTRY_REQUEST %s (%s, score=%.2f, target=%.3f)",
                candidate.symbol,
                action,
                candidate.algo_score,
                float(target_suggestion),
            )

        return ScannerTickResult(
            candidates_evaluated=len(candidates),
            entries_requested=entries_requested,
            candidates_skipped=candidates_skipped,
            used_fallback=used_fallback,
            messages_sent=messages_sent,
        )

    def _build_template_vars(
        self,
        first: CandidateInfo,
        candidates: list[CandidateInfo],
        market: MarketContext,
    ) -> dict[str, Any]:
        """Build template variables for the scanner override prompt."""
        return {
            "symbol": first.symbol,
            "headline": first.headline,
            "category": first.category,
            "subcategory": first.subcategory,
            "sentiment": first.sentiment,
            "confidence": f"{first.confidence:.2f}",
            "minutes_since": first.minutes_since_headline,
            "signal_name": first.signal_name,
            "candidate_count": len(candidates),
            "algo_action": "approve",
            "sector": first.sector,
            "sector_change": f"{market.sector_change_pct:.2f}",
            "same_symbol_today": first.same_symbol_traded_today,
            "similar_count": first.similar_headlines_last_2h,
            "spy_change": f"{market.spy_change_pct:.2f}",
            "vix": f"{market.vix:.1f}",
        }

    def _extract_decisions(
        self,
        response: LLMResponse,
        candidates: list[CandidateInfo],
    ) -> list[dict[str, Any]]:
        """Extract per-candidate decisions from LLM response.

        Falls back to approve-all if parsing fails.
        """
        parsed_candidates = response.parsed.get("candidates", [])

        if not isinstance(parsed_candidates, list) or not parsed_candidates:
            # Fallback: approve all candidates
            return [{"action": "approve", "target_pct_suggestion": 0.01}] * len(candidates)

        # Match by symbol or by index
        decisions: list[dict[str, Any]] = []
        for i, candidate in enumerate(candidates):
            matched = None
            for pc in parsed_candidates:
                if isinstance(pc, dict) and pc.get("symbol") == candidate.symbol:
                    matched = pc
                    break
            if matched is None and i < len(parsed_candidates):
                matched = parsed_candidates[i] if isinstance(parsed_candidates[i], dict) else None

            if matched:
                decisions.append(matched)
            else:
                decisions.append({"action": "approve", "target_pct_suggestion": 0.01})

        return decisions
