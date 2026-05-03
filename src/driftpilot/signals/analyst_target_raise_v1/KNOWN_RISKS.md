# Known risks — analyst_target_raise_v1

1. **Classifier accuracy is load-bearing.** The signal trusts the
   upstream catalyst classifier to label events with
   `category="analyst"`, `subcategory="target_raise"`. False positives
   (e.g. a target *cut* misclassified as a raise) will be traded as
   though the validated 1.42x@60m edge applies. There is no second
   line of defence inside this signal.

2. **The validated cell fades fast.** Forward-return ratio drops from
   1.42x at 60m to **0.97x by 1day** — essentially at parity. If the
   60-minute time stop fails to fire (clock skew, missed bars,
   harness bug) the edge has evaporated by the time we exit. The
   `time_stop` check is therefore the most operationally important
   exit branch.

3. **Thin sample (N=104).** Validation rests on 104 events across
   calendar-2024 mid-caps — smaller than the earnings/report cell and
   well below the threshold where confidence intervals tighten
   meaningfully. A single regime shift could plausibly invalidate the
   point estimate.

4. **2024 vol-regime specificity.** The horizon study is calendar-2024
   only. 2024 was a generally trending, low-VIX environment; the
   target-raise edge may not survive in choppy, high-vol regimes
   (e.g. 2022-style). Re-validation is required before deploying into
   a materially different regime.

5. **Event-age filter assumes accurate timestamps.** `is_event_fresh`
   trusts `CatalystEvent.ts` and the local clock. A delayed feed can
   admit stale events that look fresh; a fast feed with skewed
   timestamps can prematurely drop tradeable events.
