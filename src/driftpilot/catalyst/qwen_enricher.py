from __future__ import annotations
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from driftpilot.catalyst.context_assembler import EnrichmentContext

logger = logging.getLogger(__name__)

Sentiment = Literal["positive", "negative", "neutral"]


@dataclass(frozen=True)
class EnrichmentResult:
    sentiment: Sentiment
    priority_modifier: float
    horizon_override: int | None
    confidence: float = 0.5


_DEFAULT = EnrichmentResult(sentiment="neutral", priority_modifier=0.0, horizon_override=None, confidence=0.0)
_VALID_HORIZONS = {60, 240, 1440, 2880}
_VALID_SENTIMENTS = {"positive", "negative", "neutral"}

# Qwen3 is a "thinking" model: responses come wrapped in <think>...</think>{json}.
# We disable thinking via the /no_think tag and strip any residual block.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)
# Find the first JSON object in the response (after any think block).
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", flags=re.DOTALL)

_SYSTEM_PROMPT_V1 = (
    "You are a senior US equity analyst with 20 years of experience trading "
    "US stock markets. You specialize in short-term momentum — reading news "
    "headlines and rating each stock BUY, SELL, or NEUTRAL for the next 60 "
    "minutes of trading.\n\n"
    "YOUR EDGE: You understand that headlines move stocks in predictable ways:\n"
    "- Earnings beat, revenue above consensus, raised guidance → BUY (positive)\n"
    "- Earnings miss, revenue miss, lowered guidance → SELL (negative)\n"
    "- Analyst raises price target, upgrade, new buy rating → BUY (positive)\n"
    "- Analyst lowers price target, downgrade, new sell rating → SELL (negative)\n"
    "- New product launch, major partnership, strategic deal → BUY (positive)\n"
    "- CEO/executive meets world leaders, signs international deals → BUY (positive)\n"
    "- M&A target announced → BUY for target (positive)\n"
    "- Lawsuits, regulatory action, SEC investigation → SELL (negative)\n"
    "- SEC filing (8-A, 8-K), routine regulatory filing → NEUTRAL unless content is bullish/bearish\n"
    "- Roundup articles listing multiple stocks → NEUTRAL\n"
    "- Stock mentioned in passing in a broader story → NEUTRAL\n\n"
    "DECISION RULE: If the headline has a clear directional implication, "
    "COMMIT to BUY or SELL. Do NOT default to NEUTRAL out of caution — you "
    "are a trader, not a compliance officer. NEUTRAL is ONLY for headlines "
    "with genuinely no directional signal.\n\n"
    "Return ONLY a JSON object with these keys:\n"
    "- \"sentiment\": \"positive\" (BUY), \"negative\" (SELL), or \"neutral\" (HOLD)\n"
    "- \"confidence\": float 0.0-1.0 (conviction in the direction)\n"
    "- \"priority_modifier\": float -0.20 to +0.20 (expected price move magnitude: "
    "+0.15 = strong beat/upgrade, +0.05 = mild positive, -0.10 = moderate negative, "
    "0.0 = no edge)\n"
    "- \"horizon_override\": 60, 240, 1440, or 2880 if the move plays out "
    "over a different window, else null\n\n"
    "No prose, no markdown, no explanation. JSON only."
)

_SYSTEM_PROMPT_V2 = (
    "You are a senior US equity analyst with 20 years of experience trading "
    "US stock markets. You specialize in catalyst-driven momentum trades — "
    "reading news and market data to rate each stock BUY, SELL, or NEUTRAL "
    "for the next 60 minutes.\n\n"
    "You will receive a CONTEXT block with real market data. USE IT:\n"
    "- Average volume: High-volume stocks move faster on catalysts. Low-volume "
    "names (<500K avg) may not react within 60 minutes.\n"
    "- Market cap: Small/mid-caps ($500M-$10B) move more on headlines than mega-caps.\n"
    "- ATR (20-day): Tells you the stock's normal daily range. A headline that "
    "could move a 1% ATR stock ±2% is a strong signal; on a 5% ATR stock it's noise.\n"
    "- EPS/revenue beat%: Calibrate earnings headlines against actual surprise magnitude.\n"
    "- Last 4 earnings surprises: If a company routinely beats by 2%, a 2.5% beat is "
    "NOT exciting — it's priced in. Only surprises ABOVE the pattern matter.\n"
    "- VIX: Above 25 = fear regime, positive catalysts get muted. Below 15 = complacent, "
    "negative catalysts hit harder.\n"
    "- SPY change: If SPY is down >1%, even good headlines struggle. Broad market "
    "direction matters.\n"
    "- Sector ETF 5d return: Sector momentum amplifies or dampens individual catalysts.\n"
    "- Headline cluster count: If >0, this stock already had recent headlines — "
    "the move may be priced in. Reduce confidence and magnitude.\n"
    "- Minutes to open: Pre-market headlines have more drift potential than mid-day.\n\n"
    "OUTPUT FORMAT — return ONLY a JSON object with exactly these four keys:\n"
    '{"sentiment": "positive", "priority_modifier": 0.12, "confidence": 0.85, "horizon_override": null}\n\n'
    "FIELD DEFINITIONS:\n"
    '- "sentiment": "positive" (BUY), "negative" (SELL), or "neutral" (HOLD). '
    "MUST be a string, not a number.\n"
    '- "priority_modifier": float -0.20 to +0.20 — your expected price move magnitude, '
    "calibrated against the stock's ATR and volume.\n"
    '- "confidence": float 0.0 to 1.0 — conviction level.\n'
    '- "horizon_override": 60, 240, 1440, 2880, or null.\n\n'
    "MAGNITUDE TIERS for priority_modifier:\n"
    "+0.15 to +0.20: Blowout beat >5% above consensus, guidance raised, high volume name\n"
    "+0.08 to +0.14: Clear beat 2-5%, major upgrade, significant product launch\n"
    "+0.03 to +0.07: Moderate beat 1-2%, target raise, partnership announcement\n"
    "+0.01 to +0.02: Marginal positive, routine, or already in the stock's ATR noise\n"
    " 0.00: No directional signal\n"
    "-0.01 to -0.07: Small miss, minor negative, guidance maintained\n"
    "-0.08 to -0.14: Clear miss, downgrade, lawsuit filed\n"
    "-0.15 to -0.20: Large miss + guidance cut, major downgrade, serious legal action\n\n"
    "CONFIDENCE CALIBRATION:\n"
    "0.90-1.00: Numbers in headline, direction unambiguous, surprise exceeds history\n"
    "0.70-0.89: Clear direction but magnitude uncertain, or moderate catalyst\n"
    "0.50-0.69: Directional lean, context needed to confirm\n"
    "0.30-0.49: Weak signal, noise likely, slight lean\n"
    "0.00-0.29: No edge, coin flip\n\n"
    "DECISION RULE: If the headline has a clear directional implication, COMMIT "
    "to BUY or SELL. Do NOT default to NEUTRAL out of caution. NEUTRAL is ONLY "
    "for headlines with genuinely zero directional signal. You are a trader — "
    "take a position.\n\n"
    "No prose, no markdown, no explanation. JSON only."
)


def _strip_thinking_and_extract_json(content: str) -> str:
    """Strip Qwen3 <think>...</think> wrapper and return the first JSON object substring."""
    cleaned = _THINK_BLOCK_RE.sub("", content).strip()
    match = _JSON_OBJECT_RE.search(cleaned)
    return match.group(0) if match else cleaned


class QwenEnricher:
    def __init__(
        self,
        base_url: str = "http://192.168.1.166:8000/v1",
        model: str = "Qwen/Qwen3-8B",
        timeout_ms: int = 10000,
        max_tokens: int = 128,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_ms / 1000.0
        self._max_tokens = max_tokens
        self._client = client

    async def enrich(
        self,
        headline: str,
        category: str,
        subcategory: str,
        *,
        context: EnrichmentContext | None = None,
    ) -> EnrichmentResult:
        result, _ = await self.enrich_with_response(
            headline,
            category,
            subcategory,
            context=context,
        )
        return result

    async def enrich_with_response(
        self,
        headline: str,
        category: str,
        subcategory: str,
        *,
        context: EnrichmentContext | None = None,
    ) -> tuple[EnrichmentResult, dict[str, Any]]:
        user_prompt = _build_user_prompt(headline, category, subcategory, context=context)
        system_prompt = _SYSTEM_PROMPT_V2 if context is not None else _SYSTEM_PROMPT_V1
        try:
            client = self._client or httpx.AsyncClient(timeout=self._timeout_s)
            try:
                resp = await asyncio.wait_for(
                    client.post(
                        f"{self._base_url}/chat/completions",
                        json={
                            "model": self._model,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "temperature": 0.0,
                            "max_tokens": self._max_tokens,
                        },
                    ),
                    timeout=self._timeout_s,
                )
            finally:
                if self._client is None:
                    await client.aclose()

            if resp.status_code != 200:
                logger.warning("qwen status %d for headline=%r", resp.status_code, headline[:60])
                return _DEFAULT, {}

            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            json_str = _strip_thinking_and_extract_json(content)
            data = json.loads(json_str)
            return self._parse(data), data
        except (asyncio.TimeoutError, httpx.RequestError, KeyError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("qwen enrichment failed (%s): %s", type(exc).__name__, str(exc)[:120])
            return _DEFAULT, {}

    @staticmethod
    def _parse(data: dict) -> EnrichmentResult:
        sentiment = data.get("sentiment", "neutral")
        if sentiment not in _VALID_SENTIMENTS:
            # Qwen sometimes puts a numeric value in sentiment (e.g. "+0.14").
            # Try to recover: if it looks numeric, use it as pm and infer sentiment.
            try:
                misplaced_pm = float(sentiment)
                if misplaced_pm > 0.005:
                    sentiment = "positive"
                elif misplaced_pm < -0.005:
                    sentiment = "negative"
                else:
                    sentiment = "neutral"
                # Use the misplaced value as priority_modifier if pm field is empty/zero
                raw_pm = data.get("priority_modifier", 0)
                try:
                    if abs(float(raw_pm)) < 0.001:
                        data = {**data, "priority_modifier": misplaced_pm}
                except (TypeError, ValueError):
                    data = {**data, "priority_modifier": misplaced_pm}
                logger.debug("recovered sentiment from numeric value: %s -> %s", misplaced_pm, sentiment)
            except (TypeError, ValueError):
                sentiment = "neutral"

        try:
            pm = float(data.get("priority_modifier", 0.0))
        except (TypeError, ValueError):
            pm = 0.0
        pm = max(-0.2, min(0.2, pm))

        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        ho_raw = data.get("horizon_override")
        horizon_override = ho_raw if (isinstance(ho_raw, int) and ho_raw in _VALID_HORIZONS) else None

        return EnrichmentResult(
            sentiment=sentiment, priority_modifier=pm,
            horizon_override=horizon_override, confidence=confidence,
        )


_CATEGORY_HINTS: dict[str, str] = {
    "earnings/report": (
        "CATEGORY PRIOR: Earnings reports almost always move a stock. "
        "A beat → positive. A miss → negative. Only neutral if the headline "
        "gives NO indication of beat/miss (rare for earnings reports)."
    ),
    "earnings/beat": (
        "CATEGORY PRIOR: This is explicitly tagged as an earnings beat. "
        "Default sentiment is positive unless the headline mentions negative "
        "guidance or a revenue miss that outweighs the EPS beat."
    ),
    "earnings/miss": (
        "CATEGORY PRIOR: This is explicitly tagged as an earnings miss. "
        "Default sentiment is negative unless there's a strong offset "
        "(raised guidance, one-time charges excluded)."
    ),
    "earnings/guidance_up": (
        "CATEGORY PRIOR: Raised guidance is a strong positive signal. "
        "Default: positive with priority_modifier ≥ +0.08."
    ),
    "earnings/guidance_down": (
        "CATEGORY PRIOR: Lowered guidance is a strong negative signal. "
        "Default: negative with priority_modifier ≤ -0.08."
    ),
    "analyst/target_raise": (
        "CATEGORY PRIOR: An analyst RAISING a price target is ALWAYS positive. "
        "Do NOT return neutral. Even if the analyst rating is 'Maintains' or "
        "'Equal-Weight' or 'Neutral', the ACT of raising the target is bullish. "
        "sentiment MUST be \"positive\", priority_modifier +0.05 to +0.12. "
        "Only exception: if the headline explicitly says the stock is being "
        "downgraded simultaneously (extremely rare)."
    ),
    "analyst/target_cut": (
        "CATEGORY PRIOR: An analyst LOWERING a price target is ALWAYS negative. "
        "Do NOT return neutral. Even if the analyst rating is 'Maintains' or "
        "'Overweight', the ACT of cutting the target is bearish. "
        "sentiment MUST be \"negative\", priority_modifier -0.05 to -0.12."
    ),
    "analyst/upgrade": (
        "CATEGORY PRIOR: An analyst UPGRADE is ALWAYS positive. "
        "Do NOT return neutral. An upgrade means the analyst is MORE bullish. "
        "sentiment MUST be \"positive\", priority_modifier +0.08 to +0.15."
    ),
    "analyst/downgrade": (
        "CATEGORY PRIOR: An analyst DOWNGRADE is ALWAYS negative. "
        "Do NOT return neutral. A downgrade means the analyst is MORE bearish. "
        "sentiment MUST be \"negative\", priority_modifier -0.08 to -0.15."
    ),
    "m_and_a/acquires": (
        "CATEGORY PRIOR: Acquisition target usually gaps up. Acquirer is mixed. "
        "If the headline is about the TARGET company → positive."
    ),
    "filing/8a": (
        "CATEGORY PRIOR: 8-A filings are routine SEC registrations. "
        "Usually neutral unless the filing content signals dilution or a new product. "
        "Read the headline carefully — if it's just a filing notice, neutral."
    ),
    "filing/8k": (
        "CATEGORY PRIOR: 8-K filings can be anything. Read the headline for "
        "the actual content (executive change, material event, etc.)."
    ),
    "product/launch": (
        "CATEGORY PRIOR: A new product launch, platform rollout, or major "
        "feature release is typically positive — it shows innovation and "
        "potential revenue growth. Default: positive with priority_modifier "
        "+0.03 to +0.10. Only neutral if it's a minor update or negative "
        "if it signals pivot away from profitable business."
    ),
    "product/partnership": (
        "CATEGORY PRIOR: A strategic partnership, collaboration, or deal "
        "announcement is typically positive — it signals business expansion, "
        "new revenue streams, or market validation. Especially bullish if "
        "the partner is a major company (OpenAI, Google, Apple, etc.) or "
        "in a hot sector (AI, crypto). Default: positive with "
        "priority_modifier +0.03 to +0.10."
    ),
    "analyst/initiates": (
        "CATEGORY PRIOR: New coverage initiation. If the rating is "
        "Overweight/Buy/Outperform → positive (+0.05 to +0.12). "
        "If Underweight/Sell → negative. If Neutral/Hold → neutral."
    ),
    "analyst/reiterates": (
        "CATEGORY PRIOR: Analyst reiterates existing rating. Mildly "
        "directional — a reiterated Buy is mildly positive (+0.02 to +0.05), "
        "a reiterated Sell is mildly negative."
    ),
    "legal/lawsuit": (
        "CATEGORY PRIOR: Lawsuits and legal actions are typically negative "
        "for the defendant — they signal financial risk, regulatory trouble, "
        "or reputation damage. Default: negative with priority_modifier "
        "-0.03 to -0.08. Only positive if the company WON a lawsuit."
    ),
    "m_and_a/merger": (
        "CATEGORY PRIOR: Merger announcement. Target company usually gaps up "
        "(positive +0.10 to +0.20). Acquirer is context-dependent."
    ),
}


def _build_user_prompt(
    headline: str,
    category: str,
    subcategory: str,
    *,
    context: EnrichmentContext | None = None,
) -> str:
    cat_key = f"{category}/{subcategory}"
    hint = _CATEGORY_HINTS.get(cat_key, "")

    if context is None:
        hint_line = f"{hint}\n" if hint else ""
        return (
            f"/no_think Headline: \"{headline}\"\n"
            f"Category: {category}/{subcategory}\n"
            f"{hint_line}"
            f"Symbol context: this headline was tagged to a specific stock. "
            f"Will it move that stock's price in the next 60 minutes?"
        )

    # Build context block, noting which fields have real data vs unknown
    ctx_block = context.to_prompt_block()
    has_data = context.eps_beat_pct is not None or context.market_cap_m is not None

    guidance = (
        "Based on the headline, category prior, AND the context above, "
        "predict the 60-minute price direction."
        if has_data
        else "Context data is limited. Rely heavily on the headline text and "
        "category prior to determine sentiment. Do NOT default to neutral "
        "just because context is missing — the category and headline alone "
        "are often sufficient for a directional call."
    )

    parts = [
        f'/no_think Headline: "{headline}"',
        f"Category: {category}/{subcategory}",
    ]
    if hint:
        parts.append(f"\n{hint}")
    parts.append(f"\nCONTEXT:\n{ctx_block}")
    parts.append(f"\n{guidance}")
    return "\n".join(parts)
