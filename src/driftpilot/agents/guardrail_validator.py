"""Mechanical guardrail enforcement for agent decisions.

Called BEFORE any agent decision reaches the broker. Clamps values to
safe ranges and logs violations. These guardrails are NEVER overridable
by any agent, prompt, or configuration change.

Authority hierarchy: Guardrails > Algorithm > LLM Agent
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# --- HARDCODED CONSTANTS — NEVER CONFIGURABLE ---
MAX_STOP_LOSS_PCT: float = 0.015
MAX_PROFIT_CAP_PCT: float = 0.05
MAX_HOLD_MINUTES: int = 60
DAILY_LOSS_LIMIT_PCT: float = 0.03
MAX_SLOTS: int = 10
MAX_PER_SECTOR: int = 3
MAX_SIZE_MULTIPLIER: float = 2.0
MIN_SIZE_MULTIPLIER: float = 0.5
MIN_HOLD_BEFORE_AGENT_EXIT: int = 120  # seconds (2 min minimum hold)


@dataclass(frozen=True, slots=True)
class ValidatedEntry:
    """Result of guardrail validation on an entry decision."""

    symbol: str
    target_pct: float
    stop_pct: float
    size_multiplier: float
    allowed: bool
    denial_reason: str | None = None
    clamped: bool = False
    clamp_details: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedExit:
    """Result of guardrail validation on an exit decision."""

    symbol: str
    slot_id: int
    allowed: bool
    denial_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedTargetRaise:
    """Result of guardrail validation on a target raise."""

    symbol: str
    new_target_pct: float
    new_stop_pct: float
    allowed: bool
    clamped: bool = False
    clamp_details: str | None = None


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Snapshot of portfolio state for guardrail checks."""

    free_slots: int
    sector_counts: dict[str, int]  # sector → count of open positions
    daily_pnl_pct: float
    total_positions: int


class GuardrailValidator:
    """Enforces mechanical guardrails on all agent decisions.

    Called before ANY decision reaches the execution layer.
    Returns clamped/validated result. Logs violations.
    """

    def __init__(self) -> None:
        self._violations: list[dict] = []

    @property
    def violations_today(self) -> list[dict]:
        """All violations logged today (for observability)."""
        return list(self._violations)

    def validate_entry(
        self,
        symbol: str,
        target_pct: float,
        stop_pct: float,
        size_multiplier: float,
        sector: str,
        portfolio: PortfolioState,
    ) -> ValidatedEntry:
        """Validate an entry decision. Clamps or denies as needed."""

        # Hard denials (cannot proceed)
        if portfolio.free_slots <= 0:
            return ValidatedEntry(
                symbol=symbol,
                target_pct=target_pct,
                stop_pct=stop_pct,
                size_multiplier=size_multiplier,
                allowed=False,
                denial_reason="no_free_slots",
            )

        sector_count = portfolio.sector_counts.get(sector, 0)
        if sector_count >= MAX_PER_SECTOR:
            return ValidatedEntry(
                symbol=symbol,
                target_pct=target_pct,
                stop_pct=stop_pct,
                size_multiplier=size_multiplier,
                allowed=False,
                denial_reason=f"sector_cap_hit_{sector}_{sector_count}",
            )

        if portfolio.daily_pnl_pct <= -DAILY_LOSS_LIMIT_PCT:
            return ValidatedEntry(
                symbol=symbol,
                target_pct=target_pct,
                stop_pct=stop_pct,
                size_multiplier=size_multiplier,
                allowed=False,
                denial_reason=f"daily_loss_limit_{portfolio.daily_pnl_pct:.3f}",
            )

        # Clamping (allow but fix values)
        clamp_details: list[str] = []
        clamped_target = min(target_pct, MAX_PROFIT_CAP_PCT)
        clamped_stop = min(stop_pct, MAX_STOP_LOSS_PCT)
        clamped_size = max(MIN_SIZE_MULTIPLIER, min(size_multiplier, MAX_SIZE_MULTIPLIER))

        if clamped_target != target_pct:
            clamp_details.append(f"target {target_pct:.4f}→{clamped_target:.4f}")
        if clamped_stop != stop_pct:
            clamp_details.append(f"stop {stop_pct:.4f}→{clamped_stop:.4f}")
        if clamped_size != size_multiplier:
            clamp_details.append(f"size {size_multiplier:.2f}→{clamped_size:.2f}")

        was_clamped = len(clamp_details) > 0
        if was_clamped:
            self._log_violation(
                "entry_clamped", symbol, "; ".join(clamp_details)
            )

        return ValidatedEntry(
            symbol=symbol,
            target_pct=clamped_target,
            stop_pct=clamped_stop,
            size_multiplier=clamped_size,
            allowed=True,
            clamped=was_clamped,
            clamp_details="; ".join(clamp_details) if clamp_details else None,
        )

    def validate_exit(
        self,
        symbol: str,
        slot_id: int,
        hold_seconds: int,
        *,
        is_algo_exit: bool = False,
    ) -> ValidatedExit:
        """Validate an exit decision.

        Algo-triggered exits bypass the minimum hold time.
        Agent-requested exits must respect MIN_HOLD_BEFORE_AGENT_EXIT.
        """
        if is_algo_exit:
            # Algo exits are always allowed (Level 2 authority)
            return ValidatedExit(symbol=symbol, slot_id=slot_id, allowed=True)

        if hold_seconds < MIN_HOLD_BEFORE_AGENT_EXIT:
            self._log_violation(
                "exit_too_early",
                symbol,
                f"held {hold_seconds}s < min {MIN_HOLD_BEFORE_AGENT_EXIT}s",
            )
            return ValidatedExit(
                symbol=symbol,
                slot_id=slot_id,
                allowed=False,
                denial_reason=f"min_hold_not_met_{hold_seconds}s",
            )

        return ValidatedExit(symbol=symbol, slot_id=slot_id, allowed=True)

    def validate_target_raise(
        self,
        symbol: str,
        new_target_pct: float,
        new_stop_pct: float,
    ) -> ValidatedTargetRaise:
        """Validate a target raise request."""
        clamp_details: list[str] = []
        clamped_target = min(new_target_pct, MAX_PROFIT_CAP_PCT)
        # Trailing stop can't be negative
        clamped_stop = max(0.0, min(new_stop_pct, MAX_STOP_LOSS_PCT))

        if clamped_target != new_target_pct:
            clamp_details.append(f"target {new_target_pct:.4f}→{clamped_target:.4f}")
        if clamped_stop != new_stop_pct:
            clamp_details.append(f"stop {new_stop_pct:.4f}→{clamped_stop:.4f}")

        was_clamped = len(clamp_details) > 0
        if was_clamped:
            self._log_violation(
                "target_raise_clamped", symbol, "; ".join(clamp_details)
            )

        return ValidatedTargetRaise(
            symbol=symbol,
            new_target_pct=clamped_target,
            new_stop_pct=clamped_stop,
            allowed=True,
            clamped=was_clamped,
            clamp_details="; ".join(clamp_details) if clamp_details else None,
        )

    def should_force_exit_all(self, daily_pnl_pct: float) -> bool:
        """Check if daily loss limit is breached — force exit all positions."""
        return daily_pnl_pct <= -DAILY_LOSS_LIMIT_PCT

    def is_override_rate_exceeded(
        self, override_count: int, total_decisions: int, max_rate: float = 0.20
    ) -> bool:
        """Check if override rate has exceeded the safety limit."""
        if total_decisions < 5:
            # Not enough data to judge
            return False
        return (override_count / total_decisions) > max_rate

    def _log_violation(self, violation_type: str, symbol: str, detail: str) -> None:
        entry = {
            "type": violation_type,
            "symbol": symbol,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._violations.append(entry)
        logger.warning(
            "GUARDRAIL VIOLATION: %s | %s | %s", violation_type, symbol, detail
        )

    def reset_daily(self) -> None:
        """Clear daily violation log (call at session start)."""
        self._violations.clear()
