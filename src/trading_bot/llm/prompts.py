from __future__ import annotations

from trading_bot.llm.models import EveningInput, MorningInput


MORNING_SYSTEM_PROMPT = """You are a cautious paper-trading strategy analyst.
Return only a strategy artifact that matches the requested schema.
You may recommend trades, but you cannot loosen hard safety rules:
- paper trading only
- no trading when VIX is above 25
- max 5 watchlist picks
- target must be greater than entry
- stop loss must be less than entry
- risk controls are enforced by Python, not by you
"""


EVENING_SYSTEM_PROMPT = """You are a cautious paper-trading review analyst.
Analyze the day's paper trades and return only a learning artifact that matches the requested schema.
Do not recommend bypassing hard safety controls.
"""


def morning_user_prompt(payload: MorningInput) -> str:
    return (
        "Create today's paper-trading strategy config from this input.\n"
        f"{payload.model_dump_json(indent=2)}"
    )


def evening_user_prompt(payload: EveningInput) -> str:
    return (
        "Create today's paper-trading learning review from this input.\n"
        f"{payload.model_dump_json(indent=2)}"
    )

