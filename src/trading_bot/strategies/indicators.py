from __future__ import annotations

import pandas as pd


def ema(values: pd.Series, length: int) -> pd.Series:
    if length <= 0:
        raise ValueError("length must be positive")
    return values.ewm(span=length, adjust=False).mean()


def atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"missing ATR columns: {', '.join(sorted(missing))}")
    high_low = frame["high"] - frame["low"]
    high_prev_close = (frame["high"] - frame["close"].shift(1)).abs()
    low_prev_close = (frame["low"] - frame["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    return true_range.rolling(length).mean()


def average_volume(frame: pd.DataFrame, length: int = 20) -> float:
    if "volume" not in frame.columns:
        raise ValueError("missing volume column")
    return float(frame["volume"].tail(length).mean())

