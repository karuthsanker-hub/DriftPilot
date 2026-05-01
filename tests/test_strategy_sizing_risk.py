from __future__ import annotations

from trading_bot.strategies.risk import evaluate_daily_pause
from trading_bot.strategies.sizing import calculate_position_size, calculate_short_position_size


def test_position_sizing_risks_one_percent_and_caps_position_value() -> None:
    size = calculate_position_size(portfolio_value=50_000, entry_price=10, atr_value=0.50)

    assert size.shares == 500
    assert size.stop_price == 9
    assert size.position_value == 5_000


def test_position_sizing_caps_at_twenty_percent() -> None:
    size = calculate_position_size(portfolio_value=50_000, entry_price=100, atr_value=0.10)

    assert size.position_value == 10_000
    assert size.shares == 100


def test_short_position_sizing_places_stop_above_entry() -> None:
    size = calculate_short_position_size(portfolio_value=50_000, entry_price=10, atr_value=0.50)

    assert size.shares == 500
    assert size.stop_price == 11


def test_pause_decision_respects_kill_switch_first() -> None:
    decision = evaluate_daily_pause(trading_active=False, vix=12, daily_pnl_pct=0, spy_premarket_change_pct=0)

    assert decision.paused is True
    assert decision.reason == "kill switch inactive"


def test_pause_decision_allows_normal_day() -> None:
    decision = evaluate_daily_pause(trading_active=True, vix=18, daily_pnl_pct=-0.5, spy_premarket_change_pct=-0.2)

    assert decision.paused is False
