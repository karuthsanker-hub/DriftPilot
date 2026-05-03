"""Whale-Tail v1 - liquidity absorption signal."""

from __future__ import annotations

from driftpilot.signals.whale_tail_v1.config import SIGNAL_NAME, SIGNAL_VERSION
from driftpilot.signals.whale_tail_v1.signal import WhaleTailV1Signal


__all__ = ["SIGNAL_NAME", "SIGNAL_VERSION", "WhaleTailV1Signal"]
