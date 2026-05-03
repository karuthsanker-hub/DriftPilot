from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import uuid4

from .event import CatalystEvent

logger = logging.getLogger(__name__)

SubscriptionId = str
Callback = Callable[[CatalystEvent], Awaitable[None]]

@dataclass
class _Subscription:
    sub_id: SubscriptionId
    category: str | None  # None = wildcard
    subcategory: str | None  # None = wildcard
    callback: Callback


class CatalystEventBus:
    def __init__(self) -> None:
        self._subs: dict[SubscriptionId, _Subscription] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        category: str | None,
        subcategory: str | None,
        callback: Callback,
    ) -> SubscriptionId:
        sub_id = str(uuid4())
        sub = _Subscription(sub_id, category, subcategory, callback)
        async with self._lock:
            self._subs[sub_id] = sub
        return sub_id

    async def unsubscribe(self, sub_id: SubscriptionId) -> None:
        async with self._lock:
            self._subs.pop(sub_id, None)

    async def publish(self, event: CatalystEvent) -> None:
        async with self._lock:
            matched = [
                s for s in self._subs.values()
                if (s.category is None or s.category == event.category)
                and (s.subcategory is None or s.subcategory == event.subcategory)
            ]
        if not matched:
            return

        async def _run(sub: _Subscription) -> None:
            try:
                await sub.callback(event)
            except Exception as exc:
                logger.exception("subscriber %s raised: %s", sub.sub_id, exc)

        await asyncio.gather(*(_run(s) for s in matched))
