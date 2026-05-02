"""Cross-signal contracts shared by every package in the registry.

Every signal under `driftpilot.signals.<name>/` must:

- Implement `SignalProtocol` (`name`, `version`, `scan`, optional `evaluate_exit`).
- Use `BlockedReason` from this module, not raw strings.
- Return `Candidate` objects from its scan/rank step.
- Return `ExitDecision` objects from `evaluate_exit` if it has custom exit logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from driftpilot.signals.features import DEFAULT_RVOL_LOOKBACK, MinuteBar, Quote
from driftpilot.signals.regime import RegimeSnapshot
from driftpilot.states import BlockedReason


@dataclass(frozen=True, slots=True)
class Candidate:
    """Generic candidate emitted by every signal's scan step.

    `features` carries signal-specific values (z-score, ADX, EWMLR slope, RVOL, etc.).
    The allocator only reads `symbol`, `score`, `sector`, `allowed`, `blocked_reason`.
    """

    symbol: str
    score: float
    sector: str
    allowed: bool
    blocked_reason: BlockedReason | None = None
    features: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """Outcome of evaluating a position against a fresh bar.

    `exit_reason` is a free-form string per signal (`TARGET`, `STOP`, `TIME`,
    `RATCHET_STOP`, `HARD_EXIT`, `EOD_TIME`, `DISTRIBUTION_BREAK`, ...).
    `metadata` carries per-signal exit state (ratchet stage, peak P&L, etc.).
    """

    should_exit: bool
    exit_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


# Backwards-compatible structural alias used by intraday_momentum_v1.
class CandidateDecisionProtocol(Protocol):
    symbol: str
    allowed: bool
    reasons: tuple[str, ...]


@runtime_checkable
class SignalProtocol(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def scan(
        self,
        symbol_bars: Mapping[str, list[MinuteBar]],
        quotes: Mapping[str, Quote],
        spy_bars: list[MinuteBar],
        *,
        rvol_lookback: int = DEFAULT_RVOL_LOOKBACK,
    ) -> tuple[RegimeSnapshot, Sequence[Any]]: ...


def no_exit_decision() -> ExitDecision:
    """Default `evaluate_exit` outcome for signals without custom exit logic.

    Signals that rely on the runtime's default TARGET/STOP/TIME exits do not
    need to override `evaluate_exit`; the harness applies the default rules
    when this no-op is returned.
    """

    return ExitDecision(should_exit=False, exit_reason=None, metadata={})


__all__ = [
    "BlockedReason",
    "Candidate",
    "ExitDecision",
    "SignalProtocol",
    "CandidateDecisionProtocol",
    "no_exit_decision",
]
