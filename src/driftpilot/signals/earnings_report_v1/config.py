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
    # 2026-05-05: trailing (ratchet) stop. When enabled, replaces the fixed
    # profit_take with a stop that climbs as price climbs. Initial behavior:
    #   - peak < activation_pct → standard stop_loss applies (no trailing yet)
    #   - peak ≥ activation_pct → exit if current drops to (peak - distance)
    # Same-bar precedence in evaluate_all: time_stop > stop_loss > trailing.
    # Catches the asymmetric exit case: let winners run, lock in gains.
    trailing_enabled: bool = True
    trailing_activation_pct: float = 1.0   # peak must reach this before trailing kicks in
    trailing_distance_pct: float = 2.0     # trailing stop sits this far below peak
    # v3 directional gate: only candidates from events tagged with this
    # sentiment by Qwen will be admitted by `scan()`. Set to None to admit
    # all events (matches the spike behavior). Set to "positive" for the
    # validated GATED config (Jul-Dec 2024: edge_ratio=1.105, N=185).
    require_sentiment: str | None = "positive"
    # Defect #11 hardening: a positive sentiment label is not enough by
    # itself. Positive-gated candidates must also carry positive expected
    # magnitude unless explicitly disabled for forensic replay.
    require_positive_priority_modifier: bool = True
    # Optional confidence floor. A value <= 0 disables this gate. When enabled,
    # it is only applied when confidence is available from catalyst enrichment.
    min_sentiment_confidence: float = 0.0
