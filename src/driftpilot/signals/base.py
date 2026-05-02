from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from driftpilot.signals.features import DEFAULT_RVOL_LOOKBACK, MinuteBar, Quote
from driftpilot.signals.regime import RegimeSnapshot


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
