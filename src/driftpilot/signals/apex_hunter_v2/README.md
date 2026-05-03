# Apex Hunter v2.2

Institutional-drift / EWMLR signal. Hunts large-cap names whose 90-minute
exponentially weighted moving linear regression shows positive,
*accelerating* slope with strong fit (R²), high relative alpha vs SPY, and
non-trivial correlation to SPY — i.e. names that institutions are quietly
accumulating along with the broader tape.

## Entry filter chain

Order is locked; the first failing filter sets `BlockedReason`.

1. Time gate `10:30 ≤ ET ≤ 14:30` → `OUTSIDE_SCAN_WINDOW`
2. Universe (ADV ≥ 1.5M shares, $10 ≤ price ≤ $500). Allocator-side; signal
   itself relies on these being upstream.
3. EWMLR warm-up. Requires bars going back to 09:00 ET. Insufficient
   history → silently dropped (no Candidate emitted).
4. `weighted_r2 ≥ 0.35` → `R2_TOO_LOW`
5. `weighted_slope > 0` AND `acceleration ≥ 0` → `SLOPE_NEGATIVE_OR_DECELERATING`
6. `relative_alpha ≥ 1.5` → `ALPHA_TOO_LOW`
7. `correlation_to_spy ≥ 0.3` → `CORRELATION_TOO_LOW`
8. Top 1% of universe by `trend_quality_score = weighted_slope * weighted_r2`
   → `NOT_TOP_1PCT`
9. Sector cap (≤ 2 per sector) — allocator-side; reason exposed for
   completeness as `SECTOR_CAP_REACHED`.

Rank: `trend_quality_score` descending.

## Exit (three-stage Ratchet)

Per-position state lives in `position.metadata`. Stages are *one-way*; the
trailing stop *only moves up*.

| Stage | Trigger                                                 | ATR multiplier |
|-------|---------------------------------------------------------|----------------|
| 1     | initial                                                 | 2.0            |
| 2     | peak_unrealized_pct ≥ 1%                                | 1.0            |
| 3     | peak_unrealized_pct ≥ 2% **or** ET ≥ 15:00              | 0.5            |
| —     | ET ≥ 15:45 → `HARD_EXIT` regardless of stage            | —              |

`atr_at_entry` is captured in metadata at entry time (replay harness
supplies it). If absent, the exit falls back to `0.01 * entry_price` and
flags this in `KNOWN_RISKS.md`.

## Configuration

All numbers VERBATIM from the locked spec — see `config.py`. No 9-cell sweep
at this revision; the locked baseline is the only configuration. Do not
tune within a single backtest dataset.
