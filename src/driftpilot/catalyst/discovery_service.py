from __future__ import annotations
import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

FeedRunner = Callable[[], Awaitable[None]]


class DiscoveryService:
    """Orchestrates multiple news feeds. If a feed crashes, restart it after
    `restart_delay_s` without taking down sibling feeds.
    """

    def __init__(
        self,
        feeds: list[tuple[str, FeedRunner]],
        restart_delay_s: int = 60,
    ) -> None:
        self._feeds = feeds  # [(name, async_runner), ...]
        self._restart_delay_s = restart_delay_s

    async def start(self) -> None:
        await asyncio.gather(
            *(self._supervise(name, runner) for name, runner in self._feeds),
            return_exceptions=False,
        )

    async def _supervise(self, name: str, runner: FeedRunner) -> None:
        while True:
            try:
                await runner()
                logger.info("feed %s exited normally", name)
                return
            except asyncio.CancelledError:
                logger.info("feed %s cancelled", name)
                raise
            except Exception as exc:
                logger.exception("feed %s crashed: %s — restarting in %ds", name, exc, self._restart_delay_s)
                await asyncio.sleep(self._restart_delay_s)
