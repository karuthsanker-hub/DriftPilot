from __future__ import annotations

from typing import Protocol

from trading_bot.strategies.pead import SentimentResult


class SentimentScorer(Protocol):
    def classify(self, text: str) -> SentimentResult: ...


class KeywordSentimentScorer:
    positive_terms = {"beat", "beats", "raised", "growth", "profit", "strong", "positive", "above"}
    negative_terms = {"miss", "missed", "cut", "loss", "weak", "negative", "below", "decline"}

    def classify(self, text: str) -> SentimentResult:
        words = {word.strip(".,:;!?()[]{}").lower() for word in text.split()}
        positive = len(words & self.positive_terms)
        negative = len(words & self.negative_terms)
        if positive > negative:
            return SentimentResult(label="positive", score=min(0.99, 0.70 + positive * 0.05))
        if negative > positive:
            return SentimentResult(label="negative", score=min(0.99, 0.70 + negative * 0.05))
        return SentimentResult(label="neutral", score=0.50)


class FinBERTSentimentScorer:
    def __init__(self, model_name: str = "ProsusAI/finbert") -> None:
        self.model_name = model_name
        self._pipeline = None

    def classify(self, text: str) -> SentimentResult:
        if self._pipeline is None:
            from transformers import pipeline

            self._pipeline = pipeline("sentiment-analysis", model=self.model_name)
        result = self._pipeline(text[:4000])[0]
        return SentimentResult(label=str(result["label"]).lower(), score=float(result["score"]))

