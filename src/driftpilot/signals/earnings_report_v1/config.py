from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class EarningsReportConfig:
    max_hold_minutes: int = 60
    profit_take_pct: float = 1.0
    stop_loss_pct: float = 1.5
    # 2026-05-04 LIVE-PAPER ADJUSTMENT: relaxed from 60 → 240 to admit more
    # events during between-earnings-season news flow. Note: this DILUTES the
    # validated 60m cell (5.09× absolute) since the 240m cell was 3.23× —
    # still positive edge but smaller, AND the directional gate (positive
    # sentiment) was only validated against 60m absolute returns. Treat any
    # post-mod result as exploratory until re-validated.
    max_event_age_minutes: int = 240
    # v3 directional gate: only candidates from events tagged with this
    # sentiment by Qwen will be admitted by `scan()`. Set to None to admit
    # all events (matches the spike behavior). Set to "positive" for the
    # validated GATED config (Jul-Dec 2024: edge_ratio=1.105, N=185).
    require_sentiment: str | None = "positive"
