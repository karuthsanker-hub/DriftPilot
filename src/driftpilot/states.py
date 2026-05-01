from __future__ import annotations

from enum import StrEnum


class OperatorState(StrEnum):
    BOOT = "BOOT"
    MARKET_CLOSED = "MARKET_CLOSED"
    REGIME_CHECK = "REGIME_CHECK"
    SCANNING = "SCANNING"
    ALLOCATING = "ALLOCATING"
    IN_POSITION = "IN_POSITION"
    EXITING = "EXITING"
    RECYCLING = "RECYCLING"
    HALTED_PDT = "HALTED_PDT"
    HALTED_RISK = "HALTED_RISK"
    ERROR = "ERROR"
