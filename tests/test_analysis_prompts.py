from __future__ import annotations

from trading_bot.llm.analysis_prompts import monthly_review_prompt, nightly_analysis_prompt, weekly_watchlist_prompt


def test_analysis_prompts_keep_llm_out_of_execution_path() -> None:
    nightly = nightly_analysis_prompt([{"ticker": "ABC"}])
    weekly = weekly_watchlist_prompt([{"ticker": "XYZ"}])
    monthly = monthly_review_prompt([], {"pnl": 10})

    assert "analysis only" in weekly
    assert "Do not recommend bypassing safety rules" in nightly
    assert "Do not change hard risk controls" in monthly

