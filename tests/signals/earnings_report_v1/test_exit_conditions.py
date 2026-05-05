from __future__ import annotations

from datetime import datetime, timedelta, timezone

from driftpilot.signals.earnings_report_v1.config import EarningsReportConfig
from driftpilot.signals.earnings_report_v1.exits import (
    evaluate_all,
    profit_take,
    stop_loss,
    time_stop,
)


CFG = EarningsReportConfig()
NOW = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)


def test_time_stop_fires_independently() -> None:
    entry = NOW - timedelta(minutes=CFG.max_hold_minutes)
    fired, reason = time_stop(NOW, entry, CFG.max_hold_minutes)
    assert fired is True
    assert reason == "TIME_STOP"

    fired2, _ = time_stop(NOW, NOW - timedelta(minutes=10), CFG.max_hold_minutes)
    assert fired2 is False


def test_profit_take_fires_independently() -> None:
    fired, reason = profit_take(CFG.profit_take_pct, CFG.profit_take_pct)
    assert fired is True
    assert reason == "PROFIT_TAKE"

    assert profit_take(0.5, CFG.profit_take_pct) == (False, "")


def test_stop_loss_fires_independently() -> None:
    fired, reason = stop_loss(-CFG.stop_loss_pct, CFG.stop_loss_pct)
    assert fired is True
    assert reason == "STOP_LOSS"

    assert stop_loss(-0.5, CFG.stop_loss_pct) == (False, "")


def test_precedence_time_stop_wins_over_all() -> None:
    # All three trigger same bar: entry far enough back AND huge gain AND huge loss
    entry = NOW - timedelta(minutes=CFG.max_hold_minutes + 5)
    # impossible in real life to be both up and down — use up to confirm time wins
    fired, reason = evaluate_all(NOW, entry, 5.0, CFG)
    assert fired is True
    assert reason == "TIME_STOP"

    fired2, reason2 = evaluate_all(NOW, entry, -5.0, CFG)
    assert fired2 is True
    assert reason2 == "TIME_STOP"


def test_precedence_profit_take_over_stop_loss() -> None:
    # 2026-05-05: trailing_stop replaced fixed profit_take in default config.
    # This test verifies the LEGACY precedence path (trailing disabled).
    cfg = EarningsReportConfig(trailing_enabled=False)
    entry = NOW - timedelta(minutes=10)
    fired, reason = evaluate_all(NOW, entry, cfg.profit_take_pct + 0.1, cfg)
    assert (fired, reason) == (True, "PROFIT_TAKE")

    fired2, reason2 = evaluate_all(NOW, entry, -cfg.stop_loss_pct - 0.1, cfg)
    assert (fired2, reason2) == (True, "STOP_LOSS")


def test_no_exit_when_nothing_triggers() -> None:
    entry = NOW - timedelta(minutes=10)
    fired, reason = evaluate_all(NOW, entry, 0.2, CFG)
    assert fired is False
    assert reason == ""


# ---- 2026-05-05 trailing stop tests ----

from driftpilot.signals.earnings_report_v1.exits import trailing_stop


def test_trailing_stop_inactive_below_activation():
    fired, _ = trailing_stop(0.5, 0.5, activation_pct=1.0, trailing_distance_pct=2.0)
    assert fired is False


def test_trailing_stop_fires_when_drawdown_from_peak_exceeds_distance():
    # peak 5%, distance 2% → stop level = 3%; current 2.5% → drawdown breach
    fired, reason = trailing_stop(2.5, 5.0, 1.0, 2.0)
    assert fired is True
    assert reason == "TRAILING_STOP"


def test_trailing_stop_holds_when_above_stop_level():
    fired, _ = trailing_stop(9.5, 11.0, 1.0, 2.0)
    assert fired is False


def test_evaluate_all_uses_trailing_when_enabled():
    cfg = EarningsReportConfig(trailing_enabled=True, trailing_activation_pct=1.0, trailing_distance_pct=2.0)
    entry = NOW - timedelta(minutes=10)
    fired, reason = evaluate_all(NOW, entry, 2.0, cfg, peak_unrealized_pct=5.0)
    assert fired is True
    assert reason == "TRAILING_STOP"


def test_evaluate_all_uses_profit_take_when_trailing_disabled():
    cfg = EarningsReportConfig(trailing_enabled=False, profit_take_pct=1.0)
    entry = NOW - timedelta(minutes=10)
    fired, reason = evaluate_all(NOW, entry, 1.5, cfg, peak_unrealized_pct=1.5)
    assert fired is True
    assert reason == "PROFIT_TAKE"


def test_evaluate_all_stop_loss_beats_trailing():
    cfg = EarningsReportConfig(trailing_enabled=True, stop_loss_pct=1.5)
    entry = NOW - timedelta(minutes=10)
    fired, reason = evaluate_all(NOW, entry, -2.0, cfg, peak_unrealized_pct=0.5)
    assert fired is True
    assert reason == "STOP_LOSS"


def test_evaluate_all_time_stop_wins_over_trailing():
    cfg = EarningsReportConfig(trailing_enabled=True, max_hold_minutes=60)
    entry = NOW - timedelta(minutes=70)
    fired, reason = evaluate_all(NOW, entry, 10.0, cfg, peak_unrealized_pct=10.0)
    assert fired is True
    assert reason == "TIME_STOP"
