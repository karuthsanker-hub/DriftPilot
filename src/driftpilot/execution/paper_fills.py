from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from driftpilot.clock import DriftPilotClock, datetime_to_storage, require_aware
from driftpilot.settings import DriftPilotSettings


FillSide = Literal["buy", "sell"]


class FillRecordRepository(Protocol):
    def record(self, fill: PaperFill) -> Any: ...


@dataclass(frozen=True, slots=True)
class PaperFill:
    symbol: str
    side: FillSide
    quantity: float
    reference_price: float
    price: float
    slippage: float
    filled_at: datetime
    order_id: int | None = None
    broker_fill_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_aware(self.filled_at)


@dataclass(frozen=True, slots=True)
class AppliedPaperFill:
    fill: PaperFill
    resulting_quantity: float


class PaperFillEngine:
    def __init__(
        self,
        repository: Any | None = None,
        settings: DriftPilotSettings | None = None,
        *,
        clock: DriftPilotClock | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or DriftPilotSettings()
        self.clock = clock or DriftPilotClock(self.settings.timezone)

    async def apply_entry(
        self,
        *,
        symbol: str,
        quantity: float,
        reference_price: float,
        current_quantity: float = 0.0,
        order_id: int | None = None,
        filled_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AppliedPaperFill:
        fill = entry_fill(
            symbol=symbol,
            quantity=quantity,
            reference_price=reference_price,
            filled_at=filled_at or self.clock.now_utc(),
            order_id=order_id,
            metadata=metadata,
        )
        await self._persist(fill)
        return AppliedPaperFill(fill=fill, resulting_quantity=current_quantity + quantity)

    async def apply_exit(
        self,
        *,
        symbol: str,
        quantity: float,
        reference_price: float,
        current_quantity: float,
        order_id: int | None = None,
        filled_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AppliedPaperFill:
        if quantity > current_quantity:
            raise ValueError("exit quantity cannot exceed current quantity")
        fill = exit_fill(
            symbol=symbol,
            quantity=quantity,
            reference_price=reference_price,
            filled_at=filled_at or self.clock.now_utc(),
            order_id=order_id,
            metadata=metadata,
        )
        await self._persist(fill)
        return AppliedPaperFill(fill=fill, resulting_quantity=current_quantity - quantity)

    async def _persist(self, fill: PaperFill) -> None:
        recorder = _fill_recorder(self.repository)
        if recorder is None:
            return
        result = recorder(fill)
        if inspect.isawaitable(result):
            await result


def slippage_for_price(price: float) -> float:
    if price <= 0:
        raise ValueError("price must be positive")
    return max(0.02, 0.0005 * price)


calculate_slippage = slippage_for_price


def entry_fill(
    *,
    symbol: str,
    quantity: float,
    reference_price: float,
    filled_at: datetime,
    order_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> PaperFill:
    return _paper_fill(
        symbol=symbol,
        side="buy",
        quantity=quantity,
        reference_price=reference_price,
        filled_at=filled_at,
        order_id=order_id,
        metadata=metadata,
        direction=1,
    )


def exit_fill(
    *,
    symbol: str,
    quantity: float,
    reference_price: float,
    filled_at: datetime,
    order_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> PaperFill:
    return _paper_fill(
        symbol=symbol,
        side="sell",
        quantity=quantity,
        reference_price=reference_price,
        filled_at=filled_at,
        order_id=order_id,
        metadata=metadata,
        direction=-1,
    )


def _paper_fill(
    *,
    symbol: str,
    side: FillSide,
    quantity: float,
    reference_price: float,
    filled_at: datetime,
    order_id: int | None,
    metadata: dict[str, Any] | None,
    direction: Literal[-1, 1],
) -> PaperFill:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    slippage = slippage_for_price(reference_price)
    applied_metadata = {
        **(metadata or {}),
        "reference_price": reference_price,
        "slippage": slippage,
        "slippage_formula": "max(0.02,0.0005*price)",
        "filled_at": datetime_to_storage(filled_at),
    }
    return PaperFill(
        symbol=symbol.upper(),
        side=side,
        quantity=quantity,
        reference_price=reference_price,
        price=reference_price + (direction * slippage),
        slippage=slippage,
        filled_at=filled_at,
        order_id=order_id,
        metadata=applied_metadata,
    )


def _fill_recorder(repository: Any | None) -> Any | None:
    if repository is None:
        return None
    fills = getattr(repository, "fills", None)
    if fills is not None:
        for method_name in ("record", "append", "create"):
            recorder = getattr(fills, method_name, None)
            if recorder is not None:
                return recorder
    recorder = getattr(repository, "record_fill", None)
    if recorder is not None:
        return recorder
    return None
