"""DriftPilot autonomous paper-trading operator entrypoint.

python -m driftpilot.operator               # full run during market hours
python -m driftpilot.operator --once        # single deterministic cycle
python -m driftpilot.operator --mock-stream # synthetic bars, no Alpaca WS
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from driftpilot.clock import DriftPilotClock
from driftpilot.services import (
    MockBrokerReconciler,
    PaperExecutionAllocator,
    PaperPositionMonitor,
    SyntheticScannerService,
)
from driftpilot.settings import load_settings
from driftpilot.state_machine import DriftPilotStateMachine, MarketSession
from driftpilot.storage.repositories import DriftPilotRepository


class MockOpenMarketClock:
    def session(self, now: datetime | None = None) -> MarketSession:
        return MarketSession(True, "mock_regular_session")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DriftPilot operator.")
    parser.add_argument("--once", action="store_true", help="Run one deterministic cycle and exit.")
    parser.add_argument(
        "--mock-stream",
        action="store_true",
        help="Use synthetic bars instead of Alpaca WebSocket data.",
    )
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    asyncio.run(_run(args.once, args.mock_stream, args.env_file))


async def _run(once: bool, mock_stream: bool, env_file: str) -> None:
    settings = load_settings(env_file)
    clock = DriftPilotClock(settings.timezone)
    repository = DriftPilotRepository.open(settings.sqlite_path_obj, clock)
    scanner = SyntheticScannerService(repository, settings, clock=clock, universe_file="config/universe.csv")
    machine = DriftPilotStateMachine(
        repository,
        settings,
        clock=clock,
        market_clock=MockOpenMarketClock() if mock_stream else None,
        broker=MockBrokerReconciler(repository, settings),
        scanner=scanner,
        allocator=PaperExecutionAllocator(repository, settings, clock=clock),
        position_monitor=PaperPositionMonitor(repository, settings, clock=clock),
    )
    if once:
        state = await machine.run_once()
        print(f"state={state.value} sqlite={settings.sqlite_path}")
        return
    await machine.run_forever()


if __name__ == "__main__":
    main()
