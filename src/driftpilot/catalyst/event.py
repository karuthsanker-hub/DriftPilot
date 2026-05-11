from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Pillar = Literal["micro", "meso", "macro", "alpha"]


@dataclass(frozen=True)
class CatalystEvent:
    symbol: str
    category: str
    subcategory: str
    pillar: Pillar
    ts: datetime
    headline: str
    source: str
    horizon_minutes: int
    headline_hash: str
    sentiment: str | None = None
    priority_modifier: float = 0.0

    def __post_init__(self) -> None:
        if self.pillar not in {"micro", "meso", "macro", "alpha"}:
            raise ValueError(f"invalid pillar: {self.pillar}")
        if self.horizon_minutes not in {60, 240, 1440, 2880}:
            raise ValueError(f"invalid horizon_minutes: {self.horizon_minutes}")
