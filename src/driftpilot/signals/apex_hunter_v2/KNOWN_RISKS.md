# Apex Hunter v2.2 — Known Risks

## 1. EWMLR cross-check vs TradingView pending

The EWMLR implementation (`features.calculate_ewmlr`) is validated only
against hand-computed fixtures and synthetic linear/flat series. A
side-by-side cross-check against TradingView's "exponentially weighted
linear regression" indicator (or any equivalent third-party) is **pending**.
We have not "fabricated TradingView fixtures" — only synthetic fixtures with
analytic expectations are used in tests.

If TV uses a different weighting convention (e.g. `(1-α)^age` rather than
`exp(-ln(2)*age/H)`) or different normalization on R², slope magnitudes will
disagree. Sign and gross direction should match either way.

## 2. EWMLR cold-start raises rather than degrading

Per the locked spec, the first 10:30 ET cycle must be seeded with bars from
09:00 ET. If `calculate_ewmlr` is called with fewer than `half_life_mins * 2`
prices it raises `ValueError`. Callers (`signal.scan`) catch this and emit
no Candidate for the symbol — they do *not* fall back to a shorter window.
This is intentional: the EWMLR-vs-RT-bars correctness depends on warm-up.

## 3. `atr_at_entry` provisioning gap

The Ratchet exit reads `atr_at_entry` from `position.metadata`. The replay
harness's default `_open_position` does **not** populate this field; callers
that wire Apex Hunter into the harness must extend entry to capture ATR at
entry time. The exit gracefully falls back to `0.01 * entry_price` so it
won't crash, but the resulting stops will be miscalibrated. Tests pass
`atr_at_entry` explicitly.

## 4. Top-1% filter on tiny universes

When the surviving universe (post alpha + correlation gates) has fewer than
100 names the "top 1%" filter degenerates to "the single best score". This
is consistent with how percentile filters behave on small samples but is
worth flagging — backtests on narrow universes will see far fewer Apex
trades than a naive read of the spec implies.

## 5. Relative alpha vs flat SPY

When `spy_slope == 0` exactly, `relative_alpha` returns `±inf`. The signal
treats `+inf` as a passing alpha (infinitely strong) and `-inf` as failing.
This is the spec-compliant degenerate-case behavior, but in practice
`spy_slope` will rarely be *exactly* zero on real data.

## 6. Correlation requires 30 prior 1-bar SPY returns

If SPY has been streaming for fewer than 31 bars on the trading day, the
correlation filter falls back to 0.0 (which fails the 0.3 threshold). This
mirrors the EWMLR warm-up posture: refuse to score until enough history is
present.

## 7. No tuning in 2.2

There is no 9-cell sweep at this revision. The configuration in `config.py`
is the locked baseline. A future v2.3 may explore the
(R² threshold × correlation min × relative alpha min) grid; until then,
in-dataset tuning would constitute overfitting.
