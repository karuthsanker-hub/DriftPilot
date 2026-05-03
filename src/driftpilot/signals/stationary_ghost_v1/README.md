# Stationary Ghost v1

## Thesis
Stocks 2.5σ below their 15-bar intraday mean revert toward the mean within
20 minutes when ADX confirms non-trending state and pullback volume is low.

## Parameters
See `config.py`. Notable choices:
- 2.5σ threshold (not 3σ): captures more genuine noise reversion
- ADX < 20: refuses trending environments where reversion fails
- Volume ratio < 0.7 (current bar excluded from average): low-volume drift
- 0.75% target / 1.5% stop: inverted ratio, requires ~75% win rate

## Hypothesis
Mean reversion + ADX trend gate + low-volume pullback filter produces
a win rate at or above 75% — high enough to overcome the inverted
reward/risk ratio and slippage costs.

## Verdict log
- [filled in after backtest]

## Lessons
- [filled in after reading the report]
