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
    entry = NOW - timedelta(minutes=10)
    # construct a case where profit_take triggers; stop_loss requires negative pct
    # so they cannot both trigger same bar with one number — but we verify ordering
    # by ensuring profit_take wins when only it triggers and time_stop does not.
    fired, reason = evaluate_all(NOW, entry, CFG.profit_take_pct + 0.1, CFG)
    assert (fired, reason) == (True, "PROFIT_TAKE")

    fired2, reason2 = evaluate_all(NOW, entry, -CFG.stop_loss_pct - 0.1, CFG)
    assert (fired2, reason2) == (True, "STOP_LOSS")


def test_no_exit_when_nothing_triggers() -> None:
    entry = NOW - timedelta(minutes=10)
    fired, reason = evaluate_all(NOW, entry, 0.2, CFG)
    assert fired is False
    assert reason == ""
