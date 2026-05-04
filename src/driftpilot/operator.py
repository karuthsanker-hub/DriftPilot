"""DriftPilot autonomous paper-trading operator entrypoint.

python -m driftpilot.operator               # full run during market hours
python -m driftpilot.operator --once        # single deterministic cycle
python -m driftpilot.operator --mock-stream # synthetic bars, no Alpaca WS
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from driftpilot.clock import DriftPilotClock
from driftpilot.services import (
    MockBrokerReconciler,
    PaperExecutionAllocator,
    PaperPositionMonitor,
    SyntheticScannerService,
)
from driftpilot.settings import DriftPilotSettings, load_settings
from driftpilot.state_machine import DriftPilotStateMachine, MarketSession
from driftpilot.storage.repositories import DriftPilotRepository

logger = logging.getLogger(__name__)


class MockOpenMarketClock:
    def session(self, now: datetime | None = None) -> MarketSession:
        return MarketSession(True, "mock_regular_session")


def _build_catalyst_layer(settings: DriftPilotSettings):
    """Construct the v3 catalyst layer from settings.

    Returns ``(bus, universe_filter, discovery_service)`` if
    ``CATALYST_ENABLED=true``, else ``(None, None, None)``.

    The discovery_service is only constructed if Alpaca credentials are
    present; otherwise we still wire the bus + filter so the state machine
    can react to programmatically-injected events (useful for paper testing
    without live Alpaca News access).
    """
    if not settings.catalyst_enabled:
        return None, None, None

    # Local imports keep the catalyst layer out of the import graph when disabled.
    from driftpilot.catalyst.classifier import CatalystClassifier
    from driftpilot.catalyst.db import init_catalyst_schema
    from driftpilot.catalyst.discovery_service import DiscoveryService
    from driftpilot.catalyst.event_bus import CatalystEventBus
    from driftpilot.catalyst.qwen_enricher import QwenEnricher
    from driftpilot.catalyst.universe_filter import CatalystUniverseFilter

    Path(settings.catalyst_db_path).parent.mkdir(parents=True, exist_ok=True)
    init_catalyst_schema(settings.catalyst_db_path)

    bus = CatalystEventBus()
    universe_filter = CatalystUniverseFilter(
        settings.catalyst_db_path,
        lookback_minutes=settings.catalyst_universe_lookback_minutes,
    )

    feeds: list[tuple[str, callable]] = []

    if settings.alpaca_key_id and settings.alpaca_secret_key:
        from driftpilot.catalyst.feed_alpaca import AlpacaNewsFeed

        # Read universe symbols once at startup
        symbols: list[str] = []
        with open(settings.universe_file) as f:
            next(f, None)  # header
            for line in f:
                sym = line.split(",", 1)[0].strip()
                if sym:
                    symbols.append(sym)

        classifier = CatalystClassifier()
        enricher = QwenEnricher(
            base_url=settings.catalyst_qwen_url,
            model=settings.catalyst_qwen_model,
            timeout_ms=settings.catalyst_qwen_timeout_ms,
        )
        alpaca_feed = AlpacaNewsFeed(
            api_key=settings.alpaca_key_id,
            api_secret=settings.alpaca_secret_key,
            symbols=symbols,
            classifier=classifier,
            enricher=enricher,
            bus=bus,
            db_path=settings.catalyst_db_path,
            poll_interval_s=settings.catalyst_alpaca_poll_seconds,
        )
        feeds.append(("alpaca", alpaca_feed.run))

        if settings.catalyst_rss_enabled:
            from driftpilot.catalyst.feed_rss import (
                DEFAULT_FEEDS,
                RssNewsFeed,
                _load_universe,
            )

            try:
                universe_set = _load_universe(settings.universe_file)
                rss_feed = RssNewsFeed(
                    feed_urls=DEFAULT_FEEDS,
                    universe=universe_set,
                    classifier=classifier,
                    enricher=enricher,
                    bus=bus,
                    db_path=settings.catalyst_db_path,
                )
                feeds.append(("rss", rss_feed.run))
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("rss feed init failed (additive only): %s", exc)
    else:
        logger.warning(
            "catalyst enabled but ALPACA credentials missing — bus + filter will run "
            "but no live news will arrive. Inject events programmatically for testing."
        )

    discovery_service = DiscoveryService(feeds) if feeds else None
    return bus, universe_filter, discovery_service


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DriftPilot operator.")
    parser.add_argument("--once", action="store_true", help="Run one deterministic cycle and exit.")
    parser.add_argument(
        "--mock-stream",
        action="store_true",
        help="Use synthetic bars instead of Alpaca WebSocket data.",
    )
    parser.add_argument(
        "--paper-live",
        action="store_true",
        help="Submit real orders to Alpaca paper account. Catalyst-only "
        "(event-driven signals; no SIP bar stream wired yet).",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    args = parser.parse_args()
    logging.basicConfig(
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, args.log_level.upper(), logging.INFO),
    )
    # Silence chatty libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    asyncio.run(_run(args.once, args.mock_stream, args.env_file, args.paper_live))


async def _run(once: bool, mock_stream: bool, env_file: str, paper_live: bool = False) -> None:
    settings = load_settings(env_file)
    clock = DriftPilotClock(settings.timezone)
    repository = DriftPilotRepository.open(settings.sqlite_path_obj, clock)

    catalyst_bus, universe_filter, discovery_service = _build_catalyst_layer(settings)
    catalyst_db_path = settings.catalyst_db_path if settings.catalyst_enabled else None

    if paper_live:
        from driftpilot.services_live import (
            CatalystScannerService,
            LiveBrokerReconciler,
            build_live_components,
        )
        from driftpilot.signals.earnings_report_v1 import (
            EarningsReportConfig,
            EarningsReportSignal,
        )

        if catalyst_bus is None:
            raise RuntimeError(
                "--paper-live requires CATALYST_ENABLED=true (catalyst signals are "
                "the only ones wired for live order submission today)"
            )

        broker, allocator_service, monitor_service = build_live_components(
            repository, settings, clock=clock, catalyst_db_path=catalyst_db_path,
        )

        # Wire the catalyst signal directly to the bus + REST quotes for scanning.
        # Same instance is also injected into the position monitor below — both
        # SCANNING and IN_POSITION share state via the bus subscription.
        # NB: this signal is constructed here AND will be constructed again later
        # by the position monitor via get_signal(). Both share the same bus, so
        # both see the same events. The scanner instance is the one whose scan()
        # produces candidates; the get_signal() instance evaluates_exits.
        live_signal = EarningsReportSignal(
            EarningsReportConfig(require_sentiment="positive"),
            catalyst_bus,
        )
        await live_signal.subscribe()

        scanner = CatalystScannerService(
            signal=live_signal,
            quote_provider=allocator_service.broker.quote_provider,
            clock=clock,
        )
        # Inject the same signal instance into the monitor so it skips the registry
        monitor_service._signal = live_signal
        logger.warning(
            "🚨 PAPER-LIVE MODE: submitting real orders to Alpaca paper account at %s",
            settings.alpaca_paper_base_url,
        )
        logger.info(
            "live signal: earnings_report_v1 (require_sentiment=positive); "
            "scanner: CatalystScannerService (event-driven)"
        )
        # Wrap the broker so it satisfies the BrokerReconciler protocol the
        # state machine expects. AlpacaBrokerClient doesn't implement
        # reconcile_open_positions directly.
        broker_for_machine = LiveBrokerReconciler(broker, repository, settings)
    else:
        scanner = SyntheticScannerService(
            repository, settings, clock=clock, universe_file=settings.universe_file
        )
        broker_for_machine = MockBrokerReconciler(repository, settings)
        allocator_service = PaperExecutionAllocator(
            repository, settings, clock=clock, catalyst_db_path=catalyst_db_path,
        )
        monitor_service = PaperPositionMonitor(repository, settings, clock=clock)

    machine = DriftPilotStateMachine(
        repository,
        settings,
        clock=clock,
        market_clock=MockOpenMarketClock() if mock_stream else None,
        broker=broker_for_machine,
        scanner=scanner,
        allocator=allocator_service,
        position_monitor=monitor_service,
        catalyst_event_bus=catalyst_bus,
        catalyst_universe_filter=universe_filter,
    )

    # Wire the catalyst bus subscription if available
    if catalyst_bus is not None:
        await machine.subscribe_to_catalyst_bus(catalyst_bus)
        logger.info(
            "catalyst layer ENABLED: db=%s qwen=%s feeds=%s",
            settings.catalyst_db_path,
            settings.catalyst_qwen_url,
            "yes" if discovery_service is not None else "no",
        )

    if once:
        state = await machine.run_once()
        print(f"state={state.value} sqlite={settings.sqlite_path}")
        return

    # Run discovery service alongside the state machine so a feed crash
    # cannot kill the operator (DiscoveryService supervises with restarts).
    coros = [machine.run_forever()]
    if discovery_service is not None:
        coros.append(discovery_service.start())

    await asyncio.gather(*coros)


if __name__ == "__main__":
    main()
