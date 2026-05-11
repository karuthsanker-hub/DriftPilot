"""Regex parser for financial headline facts used by Qwen enrichment v2.

This module is intentionally pure: no API calls, no DB access, no LLM calls.
When a headline does not contain enough structured information, return None
fields rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


GuidanceDirection = str | None


@dataclass(frozen=True, slots=True)
class HeadlineParsed:
    eps_actual: float | None = None
    eps_estimate: float | None = None
    eps_beat_pct: float | None = None
    revenue_actual_m: float | None = None
    revenue_estimate_m: float | None = None
    revenue_beat_pct: float | None = None
    guidance_direction: GuidanceDirection = None
    is_mixed_signal: bool = False


_MONEY_RE = r"(?:-?\$?\(?|\(-?\$?)\d+(?:,\d{3})*(?:\.\d+)?\s*[KMBT]?\)?"
_EPS_RE = re.compile(
    rf"""
    (?:
        (?:adj(?:usted)?\.?\s+)?(?:diluted\s+)?(?:gaap\s+)?eps
        |
        (?:adj(?:usted)?\.?\s+)?earnings\s+per\s+share
    )
    \s+
    (?P<actual>{_MONEY_RE})
    \s+
    (?P<verb>beats?|tops?|above|miss(?:es|ed)?|below)
    \s+
    (?P<estimate>{_MONEY_RE})
    \s+
    (?:est(?:imate|imates)?|consensus)
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)
_REVENUE_RE = re.compile(
    rf"""
    (?:sales|revenue|revenues)
    \s+(?:of\s+)?
    (?P<actual>{_MONEY_RE})
    \s+
    (?P<verb>beats?|tops?|above|miss(?:es|ed)?|below)
    \s+
    (?P<estimate>{_MONEY_RE})
    \s+
    (?:est(?:imate|imates)?|consensus)
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

_GUIDANCE_UP_RE = re.compile(
    r"\b(raises?|raised|raising|boosts?|boosted|lifts?|lifted|increases?|increased|hikes?)\b"
    r".{0,80}\b(guidance|outlook|forecast|fy\d{0,4}|full[- ]year)\b"
    r"|"
    r"\b(guidance|outlook|forecast|fy\d{0,4}|full[- ]year)\b"
    r".{0,80}\b(raises?|raised|boosts?|boosted|lifts?|lifted|increases?|increased|hikes?)\b",
    flags=re.IGNORECASE,
)
_GUIDANCE_DOWN_RE = re.compile(
    r"\b(cuts?|cut|lowers?|lowered|reduces?|reduced|slashes?|slashed|warns?)\b"
    r".{0,80}\b(guidance|outlook|forecast|fy\d{0,4}|full[- ]year)\b"
    r"|"
    r"\b(guidance|outlook|forecast|fy\d{0,4}|full[- ]year)\b"
    r".{0,80}\b(cuts?|cut|lowers?|lowered|reduces?|reduced|slashes?|slashed|warns?|below)\b",
    flags=re.IGNORECASE,
)
_GUIDANCE_MAINTAINED_RE = re.compile(
    r"\b(reaffirms?|reaffirmed|maintains?|maintained|backs?|backed|confirms?|confirmed)\b"
    r".{0,80}\b(guidance|outlook|forecast|fy\d{0,4}|full[- ]year)\b"
    r"|"
    r"\b(guidance|outlook|forecast|fy\d{0,4}|full[- ]year)\b"
    r".{0,80}\b(reaffirms?|reaffirmed|maintains?|maintained|backs?|backed|confirms?|confirmed)\b",
    flags=re.IGNORECASE,
)


def parse_headline(headline: str) -> HeadlineParsed:
    """Extract EPS/revenue beat and guidance facts from a headline."""

    eps_actual: float | None = None
    eps_estimate: float | None = None
    eps_beat_pct: float | None = None
    revenue_actual_m: float | None = None
    revenue_estimate_m: float | None = None
    revenue_beat_pct: float | None = None

    eps_match = _EPS_RE.search(headline)
    if eps_match:
        eps_actual = _parse_amount(eps_match.group("actual"), default_unit_multiplier=1.0)
        eps_estimate = _parse_amount(eps_match.group("estimate"), default_unit_multiplier=1.0)
        eps_beat_pct = _beat_pct(eps_actual, eps_estimate)

    revenue_match = _REVENUE_RE.search(headline)
    if revenue_match:
        revenue_actual_m = _parse_amount(revenue_match.group("actual"), default_unit_multiplier=1.0)
        revenue_estimate_m = _parse_amount(revenue_match.group("estimate"), default_unit_multiplier=1.0)
        revenue_beat_pct = _beat_pct(revenue_actual_m, revenue_estimate_m)

    guidance_direction = _guidance_direction(headline)
    return HeadlineParsed(
        eps_actual=eps_actual,
        eps_estimate=eps_estimate,
        eps_beat_pct=eps_beat_pct,
        revenue_actual_m=revenue_actual_m,
        revenue_estimate_m=revenue_estimate_m,
        revenue_beat_pct=revenue_beat_pct,
        guidance_direction=guidance_direction,
        is_mixed_signal=(eps_beat_pct is not None and eps_beat_pct > 0 and guidance_direction == "down"),
    )


def _parse_amount(raw: str, *, default_unit_multiplier: float) -> float | None:
    """Parse a headline amount.

    Revenue outputs are in millions when suffixes are present:
    M -> millions, B -> thousands of millions, T -> millions of millions.
    EPS values normally have no suffix and stay as dollars/share.
    """

    text = raw.strip().upper().replace(",", "")
    negative = "(" in text and ")" in text
    text = text.replace("$", "").replace("(", "").replace(")", "").strip()
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()

    multiplier = default_unit_multiplier
    if text.endswith("K"):
        multiplier = 0.001
        text = text[:-1].strip()
    elif text.endswith("M"):
        multiplier = 1.0
        text = text[:-1].strip()
    elif text.endswith("B"):
        multiplier = 1_000.0
        text = text[:-1].strip()
    elif text.endswith("T"):
        multiplier = 1_000_000.0
        text = text[:-1].strip()

    try:
        value = float(text) * multiplier
    except ValueError:
        # Regex captures malformed numeric fragments as unparseable headline data.
        return None
    return -value if negative else value


def _beat_pct(actual: float | None, estimate: float | None) -> float | None:
    if actual is None or estimate is None or estimate == 0:
        return None
    return ((actual - estimate) / abs(estimate)) * 100.0


def _guidance_direction(headline: str) -> GuidanceDirection:
    if _GUIDANCE_DOWN_RE.search(headline):
        return "down"
    if _GUIDANCE_UP_RE.search(headline):
        return "up"
    if _GUIDANCE_MAINTAINED_RE.search(headline):
        return "maintained"
    return None


__all__ = ["HeadlineParsed", "parse_headline"]
