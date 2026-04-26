from __future__ import annotations

from trading_bot.strategies.momentum import MomentumInput, score_momentum


def test_momentum_scores_all_three_dimensions() -> None:
    score = score_momentum(
        MomentumInput(
            ticker="xyz",
            current_close=130,
            close_63d_ago=100,
            close_126d_ago=100,
            earnings_surprises_pct=[1, 2, -1, 3],
            roe=20,
            debt_to_equity=0.4,
            profit_margin=15,
        )
    )

    assert score.ticker == "XYZ"
    assert score.total_score == 6


def test_momentum_requires_quality_bundle_for_quality_points() -> None:
    score = score_momentum(
        MomentumInput(
            ticker="xyz",
            current_close=130,
            close_63d_ago=100,
            close_126d_ago=100,
            earnings_surprises_pct=[1, 2, -1, 3],
            roe=20,
            debt_to_equity=1.2,
            profit_margin=15,
        )
    )

    assert score.quality_score == 0
    assert score.total_score == 4

