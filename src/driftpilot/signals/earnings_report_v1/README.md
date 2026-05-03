# Earnings Report v1

## Thesis

Post-earnings reports produce a tradable short-horizon drift. Validation on
the mid-cap × full-2024 sample (`reports/catalyst_horizons_midcap_2024.json`)
shows an edge of **5.09× @ 60m, N=33** for `earnings/report` events. The
signal trades only on fresh post-earnings news delivered via the catalyst
event bus — no chart-pattern entry, no Alpaca polling. Exits cap the hold
at 60m to stay inside the validated horizon.

## Data path

The signal subscribes to `CatalystEventBus` for `(category="earnings",
subcategory="report")`. The bus is the ONLY data source. The catalyst
classifier upstream is responsible for tagging headlines.

## Parameters (locked)

| Param | Value | Source |
|---|---|---|
| `max_hold_minutes` | 60 | matches validated horizon |
| `profit_take_pct` | 1.0 | spec |
| `stop_loss_pct` | 1.5 | spec |
| `max_event_age_minutes` | 60 | edge decays past validated window |

## Exit precedence

When all three exit branches trigger on the same bar:
**time stop > profit take > stop loss**.

## Hypothesis

Symbols with a fresh `earnings/report` event drift in the direction of the
post-print reaction within a 60-minute window with positive expectancy
(5.09× edge ratio observed in the 2024 mid-cap validation, N=33). The
entry-eligibility window equals the validated horizon; positions taken on
stale events would over-extend past the edge envelope.

## Verdict log

| Date | Sample | Verdict | Notes |
|---|---|---|---|
| TBD | TBD | TBD | first live shadow run pending |
