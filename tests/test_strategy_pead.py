from __future__ import annotations

from trading_bot.strategies.pead import PEADAction, PEADInput, SentimentResult, evaluate_pead_signal


def base_payload(**overrides) -> PEADInput:
    payload = {
        "ticker": "abcd",
        "actual_eps": 1.10,
        "estimate_eps": 1.00,
        "sentiment": SentimentResult(label="positive", score=0.81),
        "analyst_count": 3,
        "market_cap_m": 800,
        "price": 12,
        "ema50": 10,
        "earnings_day_volume": 300_000,
        "avg_volume_20d": 100_000,
        "is_shortable": True,
    }
    payload.update(overrides)
    return PEADInput(**payload)


def test_pead_long_signal_when_all_filters_pass() -> None:
    signal = evaluate_pead_signal(base_payload())

    assert signal.action == PEADAction.BUY_NEXT_DAY
    assert signal.skip_reason == ""


def test_pead_short_signal_when_all_filters_pass() -> None:
    signal = evaluate_pead_signal(
        base_payload(
            actual_eps=0.90,
            estimate_eps=1.00,
            sentiment=SentimentResult(label="negative", score=0.75),
            price=8,
            ema50=10,
        )
    )

    assert signal.action == PEADAction.SHORT_NEXT_DAY


def test_pead_skips_when_short_not_available() -> None:
    signal = evaluate_pead_signal(
        base_payload(
            actual_eps=0.90,
            estimate_eps=1.00,
            sentiment=SentimentResult(label="negative", score=0.75),
            price=8,
            ema50=10,
            is_shortable=False,
        )
    )

    assert signal.action == PEADAction.SKIP
    assert signal.skip_reason == "ticker is not shortable"


def test_pead_skips_high_analyst_coverage() -> None:
    signal = evaluate_pead_signal(base_payload(analyst_count=8))

    assert signal.action == PEADAction.SKIP
    assert signal.skip_reason == "too much analyst coverage"

