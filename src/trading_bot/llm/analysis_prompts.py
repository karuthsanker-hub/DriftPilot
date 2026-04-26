from __future__ import annotations

from typing import Any


def nightly_analysis_prompt(trades_today: list[dict[str, Any]]) -> str:
    return (
        "Analyze today's paper trades. Identify which PEAD or momentum signals "
        "correlated with wins/losses, note any risk issues, and suggest research-only "
        "follow-up. Do not recommend bypassing safety rules.\n\n"
        f"TRADES_TODAY:\n{trades_today}"
    )


def weekly_watchlist_prompt(earnings_calendar: list[dict[str, Any]]) -> str:
    return (
        "Review this week's earnings calendar for PEAD research candidates. "
        "Prioritize microcap/small-cap names with low analyst coverage and likely "
        "post-earnings drift potential. This is analysis only; Python rules decide signals.\n\n"
        f"EARNINGS_CALENDAR:\n{earnings_calendar}"
    )


def monthly_review_prompt(month_trades: list[dict[str, Any]], month_summary: dict[str, Any]) -> str:
    return (
        "Review this month of PEAD and momentum paper-trading results. Identify "
        "strategy decay, regime problems, and which strategy deserves more/less research attention. "
        "Do not change hard risk controls.\n\n"
        f"MONTH_SUMMARY:\n{month_summary}\n\nMONTH_TRADES:\n{month_trades}"
    )

