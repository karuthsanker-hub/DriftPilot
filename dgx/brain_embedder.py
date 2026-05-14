"""Brain Embedder — wraps sentence-transformers for trading experience embeddings.

Converts structured trading contexts into dense vector representations
for similarity search in ChromaDB. Uses all-MiniLM-L6-v2 (22M params, ~90MB)
for speed — trading contexts are highly structured so lightweight embeddings
are sufficient.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sentence_transformers import SentenceTransformer

logger = logging.getLogger("brain.embedder")

# Default model — lightweight, fast, good enough for structured trading text
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class BrainEmbedder:
    """Embedding model wrapper for trading experiences."""

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str = "cpu"):
        logger.info("loading_embedding_model", extra={"model": model_name, "device": device})
        start = time.perf_counter()
        self.model = SentenceTransformer(model_name, device=device)
        self.dimension = self.model.get_sentence_embedding_dimension()
        elapsed = round(time.perf_counter() - start, 2)
        logger.info(
            "embedding_model_loaded",
            extra={"model": model_name, "dimension": self.dimension, "elapsed_s": elapsed},
        )

    def context_to_text(self, context: dict[str, Any]) -> str:
        """Convert a structured trading context into a text string for embedding.

        The text format is designed to capture the semantic meaning of the trading
        situation so that similar situations produce similar embeddings.
        """
        parts = []

        # Core signal info
        if symbol := context.get("symbol"):
            parts.append(f"Symbol: {symbol}")
        if signal := context.get("signal"):
            parts.append(f"Signal: {signal}")
        if category := context.get("category"):
            parts.append(f"Category: {category}")
        if headline := context.get("headline"):
            parts.append(f"Headline: {headline}")
        if sentiment := context.get("sentiment"):
            parts.append(f"Sentiment: {sentiment}")

        # Quantitative context
        if (confidence := context.get("confidence")) is not None:
            parts.append(f"Confidence: {confidence:.2f}")
        if (algo_score := context.get("algo_score")) is not None:
            parts.append(f"Algo score: {algo_score:.2f}")

        # Portfolio state
        if (daily_pnl := context.get("daily_pnl_pct")) is not None:
            direction = "up" if daily_pnl >= 0 else "down"
            parts.append(f"Daily P&L: {direction} {abs(daily_pnl):.1f}%")
        if (open_slots := context.get("open_slots")) is not None:
            parts.append(f"Open slots: {open_slots}")
        if sector := context.get("sector"):
            parts.append(f"Sector: {sector}")

        # Timing
        if (minutes := context.get("minutes_in_session")) is not None:
            if minutes < 30:
                phase = "early session (first 30 min)"
            elif minutes < 120:
                phase = "mid-morning"
            elif minutes < 240:
                phase = "afternoon"
            else:
                phase = "late session"
            parts.append(f"Session phase: {phase} ({minutes} min)")

        # Momentum/streak
        if (consec_losses := context.get("consecutive_losses")) is not None and consec_losses > 0:
            parts.append(f"Losing streak: {consec_losses} consecutive losses")
        if (consec_wins := context.get("consecutive_wins")) is not None and consec_wins > 0:
            parts.append(f"Winning streak: {consec_wins} consecutive wins")

        # Volatility
        if (rvol := context.get("rvol")) is not None:
            vol_label = "high" if rvol > 3 else "moderate" if rvol > 1.5 else "low"
            parts.append(f"Relative volume: {vol_label} ({rvol:.1f}x)")
        if (atr := context.get("atr_pct")) is not None:
            parts.append(f"ATR: {atr:.1f}%")

        return " | ".join(parts) if parts else "empty context"

    def embed_context(self, context: dict[str, Any]) -> list[float]:
        """Embed a single trading context dict."""
        text = self.context_to_text(context)
        return self.model.encode(text, normalize_embeddings=True).tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple text strings (batch)."""
        embeddings = self.model.encode(texts, normalize_embeddings=True, batch_size=64)
        return embeddings.tolist()

    def embed_contexts(self, contexts: list[dict[str, Any]]) -> list[list[float]]:
        """Embed multiple trading contexts (batch)."""
        texts = [self.context_to_text(c) for c in contexts]
        return self.embed_texts(texts)
