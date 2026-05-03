# analyst_target_raise_v1

Event-driven catalyst signal that fires on analyst price-target-raise
publications classified by the catalyst engine.

## Validation

Source: `reports/catalyst_horizons_midcap_2024.json` (mid-cap universe,
calendar-2024 backtest).

| Horizon | Forward-return ratio (mean/median) | N |
|---------|------------------------------------|---|
| 60m     | **1.42x**                          | **104** |
| 1day    | 0.97x                              | 104 |

The 60-minute cell is the only horizon where the (analyst, target_raise)
category shows a meaningful edge. The cell **fades to 0.97x by 1day**
— effectively at parity with the unconditional baseline. This means
the 60-minute hold cap is **load-bearing**: if the position is not
exited within 60 minutes the validated edge evaporates.

## Subscription

Subscribes to the `CatalystEventBus` with:
- `category="analyst"`
- `subcategory="target_raise"`

## Defaults

`AnalystTargetRaiseConfig`:
- `max_hold_minutes=60`
- `profit_take_pct=0.8`
- `stop_loss_pct=1.0`
- `max_event_age_minutes=60`

## Exit precedence

`exits.evaluate_all` checks branches in order: time stop > profit take
> stop loss. The 60-minute time stop is the primary exit per the
validation table above.

## Data sources

The signal reads ONLY from the injected `CatalystEventBus`. It never
polls Alpaca or any other market-data source directly. Position-level
P&L is read from the position object passed into `evaluate_exit`.
