"""Pydantic message schemas for A2A agent communication.

All inter-agent messages flow through these models. Each message has an
envelope (routing metadata) and a typed payload. Message types are constrained
to the 12-type protocol defined in the requirements.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    ENTRY_REQUEST = "ENTRY_REQUEST"
    ENTRY_DECISION = "ENTRY_DECISION"
    ASSIGNMENT = "ASSIGNMENT"
    TARGET_RAISE_REQUEST = "TARGET_RAISE_REQUEST"
    TARGET_RAISE_DECISION = "TARGET_RAISE_DECISION"
    PARTIAL_PROFIT_REQUEST = "PARTIAL_PROFIT_REQUEST"
    PARTIAL_PROFIT_DECISION = "PARTIAL_PROFIT_DECISION"
    EARLY_CUT_REQUEST = "EARLY_CUT_REQUEST"
    EARLY_CUT_DECISION = "EARLY_CUT_DECISION"
    FORCE_EXIT = "FORCE_EXIT"
    EXIT_REPORT = "EXIT_REPORT"
    SESSION_ADAPTATION = "SESSION_ADAPTATION"


class MessageStatus(str, Enum):
    PENDING = "pending"
    ACKED = "acked"
    PROCESSED = "processed"
    EXPIRED = "expired"


class AgentMessage(BaseModel):
    """Envelope for all A2A messages."""

    msg_id: str = Field(default_factory=lambda: str(uuid4()))
    msg_type: MessageType
    from_agent: str
    to_agent: str
    correlation_id: str | None = None
    status: MessageStatus = MessageStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processed_at: datetime | None = None
    expired_at: datetime | None = None


# --- Typed payloads for each message type ---


class EntryRequestPayload(BaseModel):
    """Payload for ENTRY_REQUEST: Scanner → PM."""

    symbol: str
    signal_name: str
    algo_score: float
    headline: str
    sentiment: str
    confidence: float
    priority_modifier: float
    proposed_target_pct: float = 0.01
    proposed_stop_pct: float = 0.015
    sector: str = ""
    catalyst_event_id: int | None = None


class EntryDecisionPayload(BaseModel):
    """Payload for ENTRY_DECISION: PM → Scanner."""

    decision: str  # "approve" | "deny"
    reasoning: str
    target_pct: float = 0.01
    size_multiplier: float = 1.0


class AssignmentPayload(BaseModel):
    """Payload for ASSIGNMENT: PM → Slot N."""

    symbol: str
    signal_name: str
    target_pct: float
    stop_pct: float
    size_multiplier: float = 1.0
    entry_request_id: str  # correlation to original ENTRY_REQUEST


class TargetRaiseRequestPayload(BaseModel):
    """Payload for TARGET_RAISE_REQUEST: Slot → PM."""

    symbol: str
    slot_id: int
    current_target_pct: float
    proposed_target_pct: float
    unrealized_pct: float
    reasoning: str
    confidence: float


class TargetRaiseDecisionPayload(BaseModel):
    """Payload for TARGET_RAISE_DECISION: PM → Slot."""

    decision: str  # "approve" | "deny"
    approved_target_pct: float | None = None
    reasoning: str


class PartialProfitRequestPayload(BaseModel):
    """Payload for PARTIAL_PROFIT_REQUEST: Slot → PM."""

    symbol: str
    slot_id: int
    unrealized_pct: float
    hold_minutes: int
    reasoning: str
    confidence: float


class PartialProfitDecisionPayload(BaseModel):
    """Payload for PARTIAL_PROFIT_DECISION: PM → Slot."""

    decision: str  # "approve" | "deny"
    reasoning: str


class EarlyCutRequestPayload(BaseModel):
    """Payload for EARLY_CUT_REQUEST: Slot → PM."""

    symbol: str
    slot_id: int
    unrealized_pct: float
    reasoning: str
    confidence: float


class EarlyCutDecisionPayload(BaseModel):
    """Payload for EARLY_CUT_DECISION: PM → Slot."""

    decision: str  # "approve" | "deny"
    reasoning: str


class ForceExitPayload(BaseModel):
    """Payload for FORCE_EXIT: PM → Slot."""

    symbol: str
    slot_id: int
    reason: str  # "daily_drawdown" | "sector_risk" | "correlation"


class ExitReportPayload(BaseModel):
    """Payload for EXIT_REPORT: Slot → PM."""

    symbol: str
    slot_id: int
    exit_reason: str
    pnl_pct: float
    hold_minutes: int
    was_override: bool = False


class SessionAdaptationPayload(BaseModel):
    """Payload for SESSION_ADAPTATION: PM → All."""

    adjustments: dict[str, float]  # param_name → new_value
    reasoning: str
    triggered_by: str  # "3_consecutive_losses" | "3_fast_wins" | etc


# --- Agent decision record (for training data export) ---


class AgentDecisionRecord(BaseModel):
    """Logged for every LLM decision — enables fine-tuning and review."""

    agent_name: str
    decision_type: str
    symbol: str | None = None
    slot_id: int | None = None
    algo_recommendation: str
    agent_decision: str
    is_override: bool = False
    reasoning: str
    confidence: float | None = None
    llm_model: str
    llm_latency_ms: int
    prompt_version: str
    inputs_json: dict[str, Any] = Field(default_factory=dict)
    raw_response: str = ""
    outcome_pnl_pct: float | None = None
    outcome_correct: bool | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
