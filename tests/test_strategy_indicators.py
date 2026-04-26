from __future__ import annotations

import pandas as pd

from trading_bot.strategies.indicators import atr, average_volume, ema


def test_ema_returns_series_with_latest_value() -> None:
    values = pd.Series([10, 11, 12, 13, 14])

    result = ema(values, 3)

    assert len(result) == 5
    assert result.iloc[-1] > result.iloc[0]


def test_atr_calculates_true_range_average() -> None:
    frame = pd.DataFrame(
        {
            "high": [11, 12, 13, 14, 15],
            "low": [9, 10, 11, 12, 13],
            "close": [10, 11, 12, 13, 14],
        }
    )

    result = atr(frame, length=3)

    assert result.iloc[-1] == 2


def test_average_volume_uses_tail_window() -> None:
    frame = pd.DataFrame({"volume": [10, 20, 30, 40]})

    assert average_volume(frame, length=2) == 35

