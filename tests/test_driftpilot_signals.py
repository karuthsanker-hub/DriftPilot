from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from driftpilot.signals.features import (
    MinuteBar,
    Quote,
    SignalFeatures,
    compute_signal_features,
    compute_session_vwap,
)
from driftpilot.signals.intraday_momentum import (
    build_intraday_momentum_queue,
    entry_filter,
)
from driftpilot.signals.regime import Regime, RegimeSnapshot, compute_market_regime
from driftpilot.signals.scoring import score_candidates


START = datetime(2026, 4, 30, 13, 30, tzinfo=UTC)


def bars_for(
    symbol: str,
    closes: list[float],
    *,
    volumes: list[float] | None = None,
    spread: float = 0.04,
) -> list[MinuteBar]:
    if volumes is None:
        volumes = [100.0 for _ in closes]
    return [
        MinuteBar(
            symbol=symbol,
            timestamp=START + timedelta(minutes=index),
            open=close,
            high=close + spread,
            low=close - spread,
            close=close,
            volume=volumes[index],
        )
        for index, close in enumerate(closes)
    ]


def rvol_history_for(symbol: str, current_timestamp: datetime, volumes: list[float]) -> list[MinuteBar]:
    return [
        MinuteBar(
            symbol=symbol,
            timestamp=current_timestamp - timedelta(days=20 - index),
            open=90.0,
            high=91.0,
            low=89.0,
            close=90.0,
            volume=volume,
        )
        for index, volume in enumerate(volumes)
    ]


def feature(
    symbol: str,
    *,
    rvol: float,
    return_15m: float,
    price: float = 102.0,
    vwap: float = 100.0,
    spread: float = 0.03,
) -> SignalFeatures:
    return SignalFeatures(
        symbol=symbol,
        timestamp=START,
        price=price,
        session_vwap=vwap,
        rvol=rvol,
        return_15m=return_15m,
        spread=spread,
        spread_limit=max(0.02, 0.001 * price),
        distance_above_vwap_pct=price / vwap - 1.0,
        has_15m_history=True,
        has_rvol_history=True,
    )


def test_signal_features_compute_vwap_rvol_15m_return_and_spread_limit() -> None:
    closes = [100.0 + index for index in range(21)]
    session_volumes = [100.0 for _ in range(20)] + [250.0]
    session_bars = bars_for("ABC", closes, volumes=session_volumes)
    history = rvol_history_for("ABC", session_bars[-1].timestamp, [100.0 for _ in range(20)])
    quote = Quote("ABC", session_bars[-1].timestamp, bid=119.95, ask=120.04)

    features = compute_signal_features(history + session_bars, quote=quote)

    expected_vwap = sum(
        bar.volume * ((bar.high + bar.low + bar.close) / 3)
        for bar in session_bars
    ) / sum(session_volumes)
    assert features.symbol == "ABC"
    assert features.price == 120.0
    assert features.session_vwap == pytest.approx(expected_vwap)
    assert features.rvol == pytest.approx(2.5)
    assert features.return_15m == pytest.approx(120.0 / 105.0 - 1.0)
    assert features.spread == pytest.approx(0.09)
    assert features.spread_limit == pytest.approx(0.12)
    assert features.spread_ok is True


def test_session_vwap_uses_typical_price_weighted_by_volume() -> None:
    bars = [
        MinuteBar("TYP", START, open=100.0, high=106.0, low=100.0, close=100.0, volume=100.0),
        MinuteBar("TYP", START + timedelta(minutes=1), open=100.0, high=101.0, low=95.0, close=101.0, volume=300.0),
    ]

    assert compute_session_vwap(bars) == pytest.approx(((102.0 * 100.0) + (99.0 * 300.0)) / 400.0)


def test_rvol_uses_same_minute_across_last_20_trading_days() -> None:
    session_bars = bars_for("RVL", [100.0 + index for index in range(21)], volumes=[100.0] * 20 + [300.0])
    same_minute_history = rvol_history_for("RVL", session_bars[-1].timestamp, [100.0 for _ in range(20)])
    wrong_minute_history = [
        MinuteBar("RVL", bar.timestamp + timedelta(minutes=1), bar.open, bar.high, bar.low, bar.close, 900.0)
        for bar in same_minute_history
    ]

    features = compute_signal_features(same_minute_history + wrong_minute_history + session_bars)

    assert features.rvol == pytest.approx(3.0)
    assert features.has_rvol_history is True


def test_rvol_requires_20_prior_same_minute_days() -> None:
    session_bars = bars_for("NEW", [100.0 + index for index in range(21)])
    history = rvol_history_for("NEW", session_bars[-1].timestamp, [100.0 for _ in range(19)])

    features = compute_signal_features(history + session_bars)

    assert features.rvol == 0.0
    assert features.has_rvol_history is False


def test_regime_green_caution_and_red_thresholds_are_deterministic() -> None:
    green_spy = bars_for("SPY", [100.0 + index * 0.02 for index in range(21)])
    caution_spy = bars_for("SPY", [100.0] * 16 + [99.94, 99.93, 99.92, 99.91, 99.90])
    red_spy = bars_for("SPY", [100.0] * 16 + [99.95, 99.9, 99.8, 99.7, 99.6])

    assert compute_market_regime(green_spy).regime == Regime.GREEN
    assert compute_market_regime(caution_spy).regime == Regime.CAUTION
    assert compute_market_regime(red_spy).regime == Regime.RED


def test_market_regime_is_spy_only_for_v1() -> None:
    green_spy = bars_for("SPY", [100.0 + index * 0.02 for index in range(21)])

    snapshot = compute_market_regime(green_spy)

    assert snapshot.regime == Regime.GREEN
    assert snapshot.spy.regime == Regime.GREEN


def test_score_candidates_recomputes_zscores_across_passing_pool() -> None:
    candidates = [
        feature("AAA", rvol=2.0, return_15m=0.006, price=101.0),
        feature("BBB", rvol=3.0, return_15m=0.010, price=103.0),
        feature("CCC", rvol=4.0, return_15m=0.008, price=102.0),
    ]

    scored = score_candidates(candidates)

    assert [candidate.symbol for candidate in scored] == ["BBB", "CCC", "AAA"]
    bbb = scored[0]
    assert bbb.score == pytest.approx(
        0.4 * bbb.rvol_zscore
        + 0.3 * bbb.return_15m_zscore
        + 0.3 * bbb.distance_above_vwap_zscore
    )


def test_entry_filter_combines_thresholds_and_green_regime() -> None:
    spy = bars_for("SPY", [100.0 + index * 0.02 for index in range(21)])
    regime = compute_market_regime(spy)

    passing = feature("PASS", rvol=2.1, return_15m=0.006)
    failing = feature("FAIL", rvol=1.9, return_15m=0.004, price=99.0, spread=0.15)

    assert entry_filter(passing, regime).allowed is True

    failed = entry_filter(failing, regime)
    assert failed.allowed is False
    assert failed.reasons == (
        "RVOL below 2.0",
        "price not above session VWAP",
        "15m return below 0.5%",
        "spread exceeds max(0.02, 0.001 * price)",
    )


def test_caution_and_red_regime_apply_relative_strength_gates() -> None:
    spy_metrics = compute_market_regime(bars_for("SPY", [100.0 + index * 0.02 for index in range(21)])).spy
    caution = RegimeSnapshot(regime=Regime.CAUTION, spy=spy_metrics)
    red = RegimeSnapshot(regime=Regime.RED, spy=spy_metrics)

    assert entry_filter(feature("CAUTION_OK", rvol=2.2, return_15m=0.020), caution).allowed is True
    assert entry_filter(feature("CAUTION_NO", rvol=2.2, return_15m=0.007), caution).allowed is False
    assert entry_filter(feature("RED_OK", rvol=2.2, return_15m=0.030), red).allowed is True
    assert entry_filter(feature("RED_NO", rvol=2.2, return_15m=0.010), red).allowed is False


def test_intraday_momentum_queue_scores_only_passing_candidates() -> None:
    spy = bars_for("SPY", [100.0 + index * 0.02 for index in range(21)])
    regime = compute_market_regime(spy)
    candidates = [
        feature("AAA", rvol=2.2, return_15m=0.006, price=101.0),
        feature("BBB", rvol=3.4, return_15m=0.012, price=103.0),
        feature("LOW", rvol=1.1, return_15m=0.012, price=104.0),
    ]

    queue = build_intraday_momentum_queue(candidates, regime)

    assert [decision.symbol for decision in queue] == ["BBB", "AAA"]
    assert all(decision.scored_candidate is not None for decision in queue)
