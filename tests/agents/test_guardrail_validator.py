"""Tests for mechanical guardrail validator."""

from __future__ import annotations

import pytest

from driftpilot.agents.guardrail_validator import (
    DAILY_LOSS_LIMIT_PCT,
    MAX_PROFIT_CAP_PCT,
    MAX_SIZE_MULTIPLIER,
    MAX_STOP_LOSS_PCT,
    MIN_HOLD_BEFORE_AGENT_EXIT,
    MIN_SIZE_MULTIPLIER,
    GuardrailValidator,
    PortfolioState,
)


@pytest.fixture
def validator():
    return GuardrailValidator()


@pytest.fixture
def healthy_portfolio():
    return PortfolioState(
        free_slots=5,
        sector_counts={"tech": 1, "healthcare": 1},
        daily_pnl_pct=0.005,
        total_positions=5,
    )


class TestEntryValidation:
    def test_valid_entry_passes(self, validator, healthy_portfolio):
        result = validator.validate_entry(
            symbol="AAPL",
            target_pct=0.01,
            stop_pct=0.015,
            size_multiplier=1.0,
            sector="tech",
            portfolio=healthy_portfolio,
        )
        assert result.allowed is True
        assert result.clamped is False

    def test_clamp_stop_above_max_pct(self, validator, healthy_portfolio):
        result = validator.validate_entry(
            symbol="AAPL",
            target_pct=0.01,
            stop_pct=MAX_STOP_LOSS_PCT + 0.01,
            size_multiplier=1.0,
            sector="tech",
            portfolio=healthy_portfolio,
        )
        assert result.allowed is True
        assert result.clamped is True
        assert result.stop_pct == MAX_STOP_LOSS_PCT

    def test_clamp_target_above_5_pct(self, validator, healthy_portfolio):
        result = validator.validate_entry(
            symbol="AAPL",
            target_pct=0.08,  # Above max
            stop_pct=0.015,
            size_multiplier=1.0,
            sector="tech",
            portfolio=healthy_portfolio,
        )
        assert result.allowed is True
        assert result.clamped is True
        assert result.target_pct == MAX_PROFIT_CAP_PCT

    def test_clamp_size_above_2x(self, validator, healthy_portfolio):
        result = validator.validate_entry(
            symbol="AAPL",
            target_pct=0.01,
            stop_pct=0.015,
            size_multiplier=3.0,  # Above max
            sector="tech",
            portfolio=healthy_portfolio,
        )
        assert result.allowed is True
        assert result.clamped is True
        assert result.size_multiplier == MAX_SIZE_MULTIPLIER

    def test_clamp_size_below_0_5x(self, validator, healthy_portfolio):
        result = validator.validate_entry(
            symbol="AAPL",
            target_pct=0.01,
            stop_pct=0.015,
            size_multiplier=0.1,  # Below min
            sector="tech",
            portfolio=healthy_portfolio,
        )
        assert result.allowed is True
        assert result.clamped is True
        assert result.size_multiplier == MIN_SIZE_MULTIPLIER

    def test_deny_no_free_slots(self, validator):
        portfolio = PortfolioState(
            free_slots=0,
            sector_counts={},
            daily_pnl_pct=0.0,
            total_positions=10,
        )
        result = validator.validate_entry(
            symbol="AAPL",
            target_pct=0.01,
            stop_pct=0.015,
            size_multiplier=1.0,
            sector="tech",
            portfolio=portfolio,
        )
        assert result.allowed is False
        assert result.denial_reason == "no_free_slots"

    def test_deny_sector_cap_hit(self, validator):
        portfolio = PortfolioState(
            free_slots=5,
            sector_counts={"tech": 3},  # At max
            daily_pnl_pct=0.0,
            total_positions=5,
        )
        result = validator.validate_entry(
            symbol="AAPL",
            target_pct=0.01,
            stop_pct=0.015,
            size_multiplier=1.0,
            sector="tech",
            portfolio=portfolio,
        )
        assert result.allowed is False
        assert "sector_cap_hit" in result.denial_reason

    def test_deny_daily_loss_limit(self, validator):
        portfolio = PortfolioState(
            free_slots=5,
            sector_counts={},
            daily_pnl_pct=-(DAILY_LOSS_LIMIT_PCT + 0.001),
            total_positions=5,
        )
        result = validator.validate_entry(
            symbol="AAPL",
            target_pct=0.01,
            stop_pct=0.015,
            size_multiplier=1.0,
            sector="tech",
            portfolio=portfolio,
        )
        assert result.allowed is False
        assert "daily_loss_limit" in result.denial_reason


class TestExitValidation:
    def test_algo_exit_always_allowed(self, validator):
        result = validator.validate_exit(
            symbol="AAPL",
            slot_id=0,
            hold_seconds=10,  # Very short
            is_algo_exit=True,
        )
        assert result.allowed is True

    def test_agent_exit_denied_if_held_too_short(self, validator):
        result = validator.validate_exit(
            symbol="AAPL",
            slot_id=0,
            hold_seconds=60,  # < 120s minimum
            is_algo_exit=False,
        )
        assert result.allowed is False
        assert "min_hold_not_met" in result.denial_reason

    def test_agent_exit_allowed_after_min_hold(self, validator):
        result = validator.validate_exit(
            symbol="AAPL",
            slot_id=0,
            hold_seconds=MIN_HOLD_BEFORE_AGENT_EXIT + 1,
            is_algo_exit=False,
        )
        assert result.allowed is True


class TestTargetRaiseValidation:
    def test_valid_target_raise(self, validator):
        result = validator.validate_target_raise(
            symbol="AAPL",
            new_target_pct=0.03,
            new_stop_pct=0.005,
        )
        assert result.allowed is True
        assert result.clamped is False
        assert result.new_target_pct == 0.03

    def test_clamp_target_above_cap(self, validator):
        result = validator.validate_target_raise(
            symbol="AAPL",
            new_target_pct=0.07,  # Above 5% cap
            new_stop_pct=0.01,
        )
        assert result.allowed is True
        assert result.clamped is True
        assert result.new_target_pct == MAX_PROFIT_CAP_PCT

    def test_clamp_negative_stop(self, validator):
        result = validator.validate_target_raise(
            symbol="AAPL",
            new_target_pct=0.03,
            new_stop_pct=-0.01,  # Negative (invalid)
        )
        assert result.allowed is True
        assert result.clamped is True
        assert result.new_stop_pct == 0.0


class TestPortfolioLevelChecks:
    def test_force_exit_all_on_daily_limit(self, validator):
        assert validator.should_force_exit_all(-(DAILY_LOSS_LIMIT_PCT + 0.001)) is True
        assert validator.should_force_exit_all(-DAILY_LOSS_LIMIT_PCT) is True
        assert validator.should_force_exit_all(-(DAILY_LOSS_LIMIT_PCT - 0.001)) is False

    def test_override_rate_not_exceeded_with_few_decisions(self, validator):
        # With < 5 decisions, rate check is skipped
        assert validator.is_override_rate_exceeded(4, 4) is False

    def test_override_rate_exceeded(self, validator):
        assert validator.is_override_rate_exceeded(5, 20) is True  # 25% > 20%

    def test_override_rate_not_exceeded(self, validator):
        assert validator.is_override_rate_exceeded(3, 20) is False  # 15% < 20%


class TestViolationLogging:
    def test_violations_logged_on_clamp(self, validator, healthy_portfolio):
        validator.validate_entry(
            symbol="AAPL",
            target_pct=0.10,
            stop_pct=0.05,
            size_multiplier=5.0,
            sector="tech",
            portfolio=healthy_portfolio,
        )
        assert len(validator.violations_today) == 1
        assert "entry_clamped" in validator.violations_today[0]["type"]

    def test_reset_daily_clears_violations(self, validator, healthy_portfolio):
        validator.validate_entry(
            symbol="AAPL",
            target_pct=0.10,
            stop_pct=0.015,
            size_multiplier=1.0,
            sector="tech",
            portfolio=healthy_portfolio,
        )
        assert len(validator.violations_today) == 1
        validator.reset_daily()
        assert len(validator.violations_today) == 0
