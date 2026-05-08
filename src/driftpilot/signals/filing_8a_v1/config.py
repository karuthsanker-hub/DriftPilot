from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Filing8AConfig:
    """Filing 8-A v1 config.

    Defaults mirror earnings_report_v1 since the validation cell shape is
    similar (60m drift, intraday absolute return). The 2024 cell at 60m
    was ratio_mean=2.05, n=256, p>1%=29%, mean|r|=1.10%. Mean abs return
    ~1.1% supports a smaller profit_take than earnings (which had 2.74%).
    """
    max_hold_minutes: int = 60
    profit_take_pct: float = 1.0
    stop_loss_pct: float = 1.5
    max_event_age_minutes: int = 240
    trailing_enabled: bool = True
    trailing_activation_pct: float = 1.0
    trailing_distance_pct: float = 2.0
    # Validation cell (2.05× ratio, n=256) was measured WITHOUT sentiment
    # filtering — filing/8a events are mostly factual/descriptive so Qwen
    # tags ~90% neutral. Gating on positive kills all flow. Accept any
    # non-negative event (positive or neutral). Negative events are
    # excluded as a safety filter only.
    require_sentiment: str | None = None
    exclude_negative: bool = True
