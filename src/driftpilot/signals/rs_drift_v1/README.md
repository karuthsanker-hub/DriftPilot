# RS-Drift v1.1

## Thesis
Stocks showing relative strength vs SPY between 9:30–10:00 ET exhibit
institutional accumulation that drifts through midday and into the close.
Late entry (10:00 AM) reduces slippage drag relative to 9:30 AM strategies.

## Parameters
See `config.py`. Notable choices:
- 5×$2k slots: concentration over diversification (KNOWN_RISKS #3)
- Mid-price limit entry: avoid spread cost (KNOWN_RISKS #1)
- 1.5% target / 0.75% stop with break-even trigger: 2:1 R:R
- EOD time stop at 15:55 ET (KNOWN_RISKS #2)
- +$125 daily profit cap (KNOWN_RISKS #4)
- SPY heat tightening on 0.5% drop in 5 min (KNOWN_RISKS #5)

## Hypothesis
2:1 reward-risk plus break-even trigger produces positive expectancy at
win rates as low as ~42% before slippage. Required actual win rate
emerges from the report's `edge_ratio` calculation.

## Verdict log
- [filled in after backtest]

## Lessons
- [filled in after reading the report]
