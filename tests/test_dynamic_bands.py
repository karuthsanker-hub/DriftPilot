"""Tests for compute_dynamic_bands() — adaptive entry/exit price bands.

Verifies ATR fallback, drift tax, RVOL boost, beta profile, catalyst
profile, time-of-day adjustments, guardrail clamping, and spread cost.
"""

from __future__ import annotations

import pytest

from driftpilot.services_live import (
    BASE_STOP_PCT,
    BASE_TARGET_PCT,
    DEFAULT_ATR_PCT,
    MAX_STOP_LOSS_PCT,
    ATR_TARGET_SCALE,
    ATR_STOP_SCALE,
    DRIFT_TAX_FACTOR,
    RVOL_BOOST_FACTOR,
    BETA_WIDEN_FACTOR,
    HIGH_BETA_THRESHOLD,
    CATALYST_WIDEN,
    TIME_OF_DAY_STOP_MULT,
    DynamicBands,
    compute_dynamic_bands,
)


# -------------------------------------------------------------------
# 1. Default bands when ATR is missing (should use 1.2% default)
# -------------------------------------------------------------------
class TestDefaultATRMissing:
    def test_uses_default_atr(self):
        bands = compute_dynamic_bands()
        expected_target = DEFAULT_ATR_PCT * ATR_TARGET_SCALE
        expected_stop = DEFAULT_ATR_PCT * ATR_STOP_SCALE
        assert bands.target_pct == pytest.approx(expected_target, abs=1e-6)
        assert bands.stop_pct == pytest.approx(expected_stop, abs=1e-6)

    def test_reasoning_mentions_default(self):
        bands = compute_dynamic_bands()
        assert "ATR missing" in bands.reasoning
        assert "default" in bands.reasoning.lower()


# -------------------------------------------------------------------
# 2. ATR-based bands: 2% ATR should be wider than default 1.2%
# -------------------------------------------------------------------
class TestATRBased:
    def test_wider_bands_for_higher_atr(self):
        default_bands = compute_dynamic_bands()
        atr_bands = compute_dynamic_bands(atr_pct=0.02)

        assert atr_bands.target_pct > default_bands.target_pct
        assert atr_bands.stop_pct > default_bands.stop_pct

    def test_atr_numeric_values(self):
        bands = compute_dynamic_bands(atr_pct=0.02)
        assert bands.target_pct == pytest.approx(0.02 * ATR_TARGET_SCALE, abs=1e-6)
        assert bands.stop_pct == pytest.approx(0.02 * ATR_STOP_SCALE, abs=1e-6)

    def test_reasoning_shows_atr(self):
        bands = compute_dynamic_bands(atr_pct=0.02)
        assert "ATR" in bands.reasoning
        assert "2.00%" in bands.reasoning


# -------------------------------------------------------------------
# 3. Drift tax: 3% drift should reduce target
# -------------------------------------------------------------------
class TestDriftTax:
    def test_drift_reduces_target(self):
        no_drift = compute_dynamic_bands(atr_pct=0.02)
        with_drift = compute_dynamic_bands(atr_pct=0.02, drift_pct=0.03)

        assert with_drift.target_pct < no_drift.target_pct

    def test_drift_tax_numeric(self):
        """3% drift on 2% ATR: tax = 0.9%, base target = 1%, result floored at 0.2%."""
        bands = compute_dynamic_bands(atr_pct=0.02, drift_pct=0.03)
        base_target = 0.02 * ATR_TARGET_SCALE  # 0.01
        tax = 0.03 * DRIFT_TAX_FACTOR          # 0.009
        raw = base_target - tax                 # 0.001 — below 0.002 floor
        expected = max(raw, 0.002)
        assert bands.target_pct == pytest.approx(expected, abs=1e-6)

    def test_drift_does_not_affect_stop(self):
        no_drift = compute_dynamic_bands(atr_pct=0.02)
        with_drift = compute_dynamic_bands(atr_pct=0.02, drift_pct=0.03)
        assert with_drift.stop_pct == no_drift.stop_pct

    def test_reasoning_mentions_drift(self):
        bands = compute_dynamic_bands(atr_pct=0.02, drift_pct=0.03)
        assert "drift tax" in bands.reasoning
        assert "drift=3.0%" in bands.reasoning

    def test_target_has_floor(self):
        """Even extreme drift should not push target below 0.2%."""
        bands = compute_dynamic_bands(atr_pct=0.01, drift_pct=0.20)
        assert bands.target_pct >= 0.002


# -------------------------------------------------------------------
# 4. RVOL conviction boost: RVOL=3x should widen target
# -------------------------------------------------------------------
class TestRVOLBoost:
    def test_rvol_widens_target(self):
        normal = compute_dynamic_bands(atr_pct=0.02, rvol=1.0)
        boosted = compute_dynamic_bands(atr_pct=0.02, rvol=3.0)

        assert boosted.target_pct > normal.target_pct

    def test_rvol_boost_numeric(self):
        bands = compute_dynamic_bands(atr_pct=0.02, rvol=3.0)
        base_target = 0.02 * ATR_TARGET_SCALE
        boost = (3.0 - 1.0) * RVOL_BOOST_FACTOR
        assert bands.target_pct == pytest.approx(base_target + boost, abs=1e-6)

    def test_rvol_does_not_affect_stop(self):
        normal = compute_dynamic_bands(atr_pct=0.02, rvol=1.0)
        boosted = compute_dynamic_bands(atr_pct=0.02, rvol=3.0)
        assert boosted.stop_pct == normal.stop_pct

    def test_rvol_at_1x_no_boost(self):
        bands = compute_dynamic_bands(atr_pct=0.02, rvol=1.0)
        assert "RVOL boost" not in bands.reasoning

    def test_reasoning_mentions_rvol(self):
        bands = compute_dynamic_bands(atr_pct=0.02, rvol=3.0)
        assert "RVOL boost" in bands.reasoning
        assert "3.0x" in bands.reasoning


# -------------------------------------------------------------------
# 5. Beta profile: high beta (>1.5) should have wider bands
# -------------------------------------------------------------------
class TestBetaProfile:
    def test_high_beta_widens_both_bands(self):
        low_beta = compute_dynamic_bands(atr_pct=0.02, beta=1.0)
        high_beta = compute_dynamic_bands(atr_pct=0.02, beta=1.8)

        assert high_beta.target_pct > low_beta.target_pct
        assert high_beta.stop_pct > low_beta.stop_pct

    def test_high_beta_numeric(self):
        bands = compute_dynamic_bands(atr_pct=0.02, beta=1.8)
        base_target = 0.02 * ATR_TARGET_SCALE * (1 + BETA_WIDEN_FACTOR)
        base_stop = 0.02 * ATR_STOP_SCALE * (1 + BETA_WIDEN_FACTOR)
        assert bands.target_pct == pytest.approx(base_target, abs=1e-6)
        assert bands.stop_pct == pytest.approx(base_stop, abs=1e-6)

    def test_beta_at_threshold_no_widen(self):
        bands = compute_dynamic_bands(atr_pct=0.02, beta=HIGH_BETA_THRESHOLD)
        assert "high-beta" not in bands.reasoning

    def test_reasoning_mentions_beta(self):
        bands = compute_dynamic_bands(atr_pct=0.02, beta=1.8)
        assert "high-beta" in bands.reasoning
        assert "1.80" in bands.reasoning


# -------------------------------------------------------------------
# 6. Catalyst profile: earnings wider than analyst
# -------------------------------------------------------------------
class TestCatalystProfile:
    def test_earnings_wider_than_analyst(self):
        earnings = compute_dynamic_bands(atr_pct=0.02, catalyst="earnings")
        analyst = compute_dynamic_bands(atr_pct=0.02, catalyst="analyst")

        assert earnings.target_pct > analyst.target_pct
        assert earnings.stop_pct > analyst.stop_pct

    def test_earnings_numeric(self):
        bands = compute_dynamic_bands(atr_pct=0.02, catalyst="earnings")
        base_target = 0.02 * ATR_TARGET_SCALE * (1 + CATALYST_WIDEN["earnings"])
        base_stop = 0.02 * ATR_STOP_SCALE * (1 + CATALYST_WIDEN["earnings"])
        assert bands.target_pct == pytest.approx(base_target, abs=1e-6)
        assert bands.stop_pct == pytest.approx(base_stop, abs=1e-6)

    def test_no_catalyst_no_widen(self):
        bands = compute_dynamic_bands(atr_pct=0.02, catalyst=None)
        assert "catalyst" not in bands.reasoning

    def test_unknown_catalyst_no_widen(self):
        bands = compute_dynamic_bands(atr_pct=0.02, catalyst="unknown_type")
        assert "catalyst" not in bands.reasoning

    def test_reasoning_mentions_catalyst(self):
        bands = compute_dynamic_bands(atr_pct=0.02, catalyst="earnings")
        assert "catalyst" in bands.reasoning
        assert "earnings" in bands.reasoning


# -------------------------------------------------------------------
# 7. Time-of-day profile: opening should have wider stops
# -------------------------------------------------------------------
class TestTimeOfDay:
    def test_open_wider_stop_than_morning(self):
        open_bands = compute_dynamic_bands(atr_pct=0.02, time_of_day="open")
        morning_bands = compute_dynamic_bands(atr_pct=0.02, time_of_day="morning")

        assert open_bands.stop_pct > morning_bands.stop_pct

    def test_open_stop_numeric(self):
        bands = compute_dynamic_bands(atr_pct=0.02, time_of_day="open")
        expected_stop = 0.02 * ATR_STOP_SCALE * TIME_OF_DAY_STOP_MULT["open"]
        assert bands.stop_pct == pytest.approx(expected_stop, abs=1e-6)

    def test_target_not_affected_by_time(self):
        open_bands = compute_dynamic_bands(atr_pct=0.02, time_of_day="open")
        morning_bands = compute_dynamic_bands(atr_pct=0.02, time_of_day="morning")
        assert open_bands.target_pct == morning_bands.target_pct

    def test_reasoning_mentions_time_of_day(self):
        bands = compute_dynamic_bands(atr_pct=0.02, time_of_day="open")
        assert "time_profile=open" in bands.reasoning


# -------------------------------------------------------------------
# 8. Guardrail clamping: stop never exceeds MAX_STOP_LOSS_PCT (3%)
# -------------------------------------------------------------------
class TestGuardrailClamping:
    def test_stop_never_exceeds_max(self):
        """Combine high ATR + high beta + earnings + open to push stop past 3%."""
        bands = compute_dynamic_bands(
            atr_pct=0.05,
            beta=2.0,
            catalyst="fda",
            time_of_day="open",
        )
        assert bands.stop_pct <= MAX_STOP_LOSS_PCT

    def test_stop_clamped_to_exact_max(self):
        bands = compute_dynamic_bands(
            atr_pct=0.05,
            beta=2.0,
            catalyst="fda",
            time_of_day="open",
        )
        assert bands.stop_pct == pytest.approx(MAX_STOP_LOSS_PCT, abs=1e-6)

    def test_reasoning_mentions_clamped(self):
        bands = compute_dynamic_bands(
            atr_pct=0.05,
            beta=2.0,
            catalyst="fda",
            time_of_day="open",
        )
        assert "clamped" in bands.reasoning

    def test_no_clamp_when_under_max(self):
        bands = compute_dynamic_bands(atr_pct=0.02)
        assert "clamped" not in bands.reasoning
        assert bands.stop_pct < MAX_STOP_LOSS_PCT


# -------------------------------------------------------------------
# 9. Spread cost deduction
# -------------------------------------------------------------------
class TestSpreadCost:
    def test_spread_reduces_target(self):
        no_spread = compute_dynamic_bands(atr_pct=0.02)
        with_spread = compute_dynamic_bands(atr_pct=0.02, spread_pct=0.002)

        assert with_spread.target_pct < no_spread.target_pct

    def test_spread_numeric(self):
        bands = compute_dynamic_bands(atr_pct=0.02, spread_pct=0.002)
        base_target = 0.02 * ATR_TARGET_SCALE
        expected = base_target - 0.002
        assert bands.target_pct == pytest.approx(expected, abs=1e-6)

    def test_spread_does_not_affect_stop(self):
        no_spread = compute_dynamic_bands(atr_pct=0.02)
        with_spread = compute_dynamic_bands(atr_pct=0.02, spread_pct=0.002)
        assert with_spread.stop_pct == no_spread.stop_pct

    def test_target_has_floor_with_spread(self):
        """Huge spread should not push target below 0.1%."""
        bands = compute_dynamic_bands(atr_pct=0.01, spread_pct=0.05)
        assert bands.target_pct >= 0.001

    def test_reasoning_mentions_spread(self):
        bands = compute_dynamic_bands(atr_pct=0.02, spread_pct=0.002)
        assert "spread cost" in bands.reasoning


# -------------------------------------------------------------------
# 10. Combined scenario: multiple adjustments stack correctly
# -------------------------------------------------------------------
class TestCombined:
    def test_all_adjustments_applied(self):
        bands = compute_dynamic_bands(
            atr_pct=0.02,
            drift_pct=0.01,
            rvol=2.0,
            beta=1.8,
            catalyst="earnings",
            time_of_day="open",
            spread_pct=0.001,
        )
        # All adjustment reasons should appear
        assert "ATR" in bands.reasoning
        assert "drift tax" in bands.reasoning
        assert "RVOL boost" in bands.reasoning
        assert "high-beta" in bands.reasoning
        assert "catalyst" in bands.reasoning
        assert "time_profile=" in bands.reasoning
        assert "spread cost" in bands.reasoning
        # Both values are positive
        assert bands.target_pct > 0
        assert bands.stop_pct > 0

    def test_return_type_is_dynamic_bands(self):
        bands = compute_dynamic_bands()
        assert isinstance(bands, DynamicBands)
        assert isinstance(bands.target_pct, float)
        assert isinstance(bands.stop_pct, float)
        assert isinstance(bands.reasoning, str)
