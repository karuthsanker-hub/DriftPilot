"""Earnings Report v1 — catalyst-driven post-earnings drift signal."""

from __future__ import annotations

from driftpilot.signals.earnings_report_v1.config import EarningsReportConfig
from driftpilot.signals.earnings_report_v1.signal import EarningsReportSignal

SIGNAL_NAME = "earnings_report_v1"
SIGNAL_VERSION = "1.0.0"

__all__ = [
    "SIGNAL_NAME",
    "SIGNAL_VERSION",
    "EarningsReportConfig",
    "EarningsReportSignal",
]
