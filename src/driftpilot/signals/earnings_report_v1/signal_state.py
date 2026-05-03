from __future__ import annotations
from datetime import datetime
from typing import TypedDict


class EarningsReportState(TypedDict, total=False):
    entry_ts: datetime
    entry_price: float
    peak_unrealized_pct: float
    catalyst_event_ts: datetime


__all__ = ["EarningsReportState"]
