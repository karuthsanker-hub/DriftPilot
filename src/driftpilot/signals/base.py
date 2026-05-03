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


class InsufficientDataError(Exception):
    """Raised by a signal when its declared data dependencies cannot be
    satisfied at the current backtest cycle.

    Per refactor plan v1.1 § 3 Task 2.2 + 2.3, signals that declare
    `data_dependencies()` MUST verify them at the start of `scan` /
    `rank_candidates` and raise `InsufficientDataError` rather than
    silently returning empty candidates or zero-dividing on a missing
    SPY bar. The backtest harness catches this, logs a
    `data_dependency_skip` event in the report's diagnostics block, and
    returns an empty candidate list for that cycle (without crashing).
    """


def signal_data_dependencies(signal: object) -> tuple[str, ...]:
    """Read a signal's declared data dependencies, with a permissive default.

    Returns the result of `signal.data_dependencies()` if the method exists,
    or an empty tuple. Used by the harness validator (Phase 2.1) without
    requiring every existing signal to immediately implement the method.
    Signals that genuinely depend on SPY (RS-Drift, Apex Hunter) override.
    """

    method = getattr(signal, "data_dependencies", None)
    if method is None:
        return ()
    result = method() if callable(method) else method
    return tuple(result)


def signal_required_history_minutes(signal: object) -> int:
    """Read a signal's declared required-history-minutes, default 0.

    Returns `signal.required_history_minutes()` if defined, else 0.
    Apex Hunter declares 180 (90-min EWMLR + warm-up); Stationary Ghost
    declares ~30. Other signals fall back to 0 until they opt in.
    """

    method = getattr(signal, "required_history_minutes", None)
    if method is None:
        return 0
    return int(method() if callable(method) else method)


__all__ = [
    "BlockedReason",
    "Candidate",
    "ExitDecision",
    "InsufficientDataError",
    "SignalProtocol",
    "CandidateDecisionProtocol",
    "no_exit_decision",
    "signal_data_dependencies",
    "signal_required_history_minutes",
]
