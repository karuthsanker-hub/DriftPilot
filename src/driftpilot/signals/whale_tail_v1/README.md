# Whale-Tail v1.1

## Thesis
Capture intraday liquidity absorption — high relative volume traded within a
compressed price range, the signature of institutional accumulation that
absorbs supply without driving price. When such absorption holds price near
the upper end of its recent compression range and price has not broken
below the compression floor, a directional move is more likely to follow.

## Parameters
See `config.py`. Notable choices (verbatim from the locked spec):
- RVOL > 3.0, with the current bar EXCLUDED from the lookback average
- 15-bar compression window, compression_score < 0.5
- range_position > 0.75 (price near the top of the compression box)
- ATR(20) Wilder's smoothing
- 1.5x ATR target / 0.75x ATR stop (R:R = 2.0)
- 60-minute time stop
- 10:00–15:00 ET scan window
- Variant B entry: marketable limit at compression high + slippage

## Hypothesis
Sustained absorption at high relative volume inside a tight range, with
price pinned at the upper boundary, identifies the institutional footprint
that precedes a directional break. With a 2:1 R:R, breakeven win rate is
roughly 33–40% after slippage.

## Verdict log
- [filled in after backtest]

## Lessons
- [filled in after reading the report]
