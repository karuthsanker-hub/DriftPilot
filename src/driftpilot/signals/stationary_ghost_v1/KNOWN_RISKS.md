# Stationary Ghost v1 — Known Risks

## 1. Inverted reward/risk ratio
0.75% target / 1.5% stop requires ~75% win rate to break even after
slippage. If actual win rate < 70%, the strategy is structurally
unprofitable.

## 2. ADX implementation correctness
Wilder's 1978 ADX has multiple incorrect implementations in public code.
Validated against:
- Wilder's original 1978 book worked example (citable)
- Invariants: flat bars → ADX < 10; trending bars → ADX > 30

TradingView cross-check pending; user owns final verification before merge
per spec section 7.2.

## 3. Volume ratio lookahead trap
PULLBACK_VOLUME_RATIO_MAX must compare current bar to average of
PRECEDING bars. Including current bar in its own denominator is silent
lookahead bias. Enforced by unit test `test_relative_volume_excludes_current_bar`.

## 4. ADX threshold is a knob
Default 20 chosen as middle of [15, 20, 25] sweep range. Do NOT optimize
within a single backtest dataset — that is overfitting. Sweep deferred to v2.

## 5. "Stock green on day" filter is unvalidated
Untested assumption. v2 may run with filter removed.

## 6. 2.5σ events still include real news
Some 2.5σ moves are repricings, not noise. Track loss tail in report.

## 7. Marketable-limit baseline only
v1 runs Variant B only. Passive-fill Variant A deferred.
