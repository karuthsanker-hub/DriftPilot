"""DriftPilot autonomous paper-trading operator entrypoint.

python -m driftpilot.operator               # full run during market hours
python -m driftpilot.operator --once        # single deterministic cycle
python -m driftpilot.operator --mock-stream # synthetic bars, no Alpaca WS
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

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
    from driftpilot.catalyst.context_assembler import ContextAssembler
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

    feeds: list[tuple[str, Callable[[], Awaitable[None]]]] = []

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
        # Context assembler gives Qwen market data (volume, VIX, ATR, etc.)
        # so it uses the V2 prompt with full analyst context.
        # enable_external_fetch=True lets yfinance fill market_cap, avg_volume,
        # and sector ETF returns when no dedicated provider is wired in.
        ctx_assembler = ContextAssembler(
            db_path=settings.catalyst_db_path,
            universe_csv_path=settings.universe_file,
            bar_root=settings.parquet_bar_root,
            enable_external_fetch=True,
        )
        try:
            ctx_assembler.cache_run_context()
            logger.info("context assembler: run-context cached (VIX, SPY, sector ETFs)")
        except Exception:
            logger.warning("context assembler run-context cache failed (non-fatal)")
        alpaca_feed = AlpacaNewsFeed(
            api_key=settings.alpaca_key_id,
            api_secret=settings.alpaca_secret_key,
            symbols=symbols,
            classifier=classifier,
            enricher=enricher,
            bus=bus,
            db_path=settings.catalyst_db_path,
            poll_interval_s=settings.catalyst_alpaca_poll_seconds,
            context_assembler=ctx_assembler,
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
                    context_assembler=ctx_assembler,
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

    # Agent orchestrator — no-op when AGENT_ENABLED=false
    from driftpilot.agents.factory import build_orchestrator

    orchestrator = build_orchestrator(settings)
    orchestrator.start()

    # Market-data adapter for agent snapshots (best-effort, graceful with None)
    from driftpilot.agents.market_data_adapter import MarketDataAdapter

    catalyst_bus, universe_filter, discovery_service = _build_catalyst_layer(settings)
    catalyst_db_path = settings.catalyst_db_path if settings.catalyst_enabled else None

    if paper_live:
        from driftpilot.services_live import (
            CatalystScannerService,
            LiveBrokerReconciler,
            MultiSignal,
            build_live_components,
        )
        from driftpilot.signals.earnings_report_v1 import (
            EarningsReportConfig,
            EarningsReportSignal,
        )
        from driftpilot.signals.analyst_target_raise_v1 import (
            AnalystTargetRaiseConfig,
            AnalystTargetRaiseV1Signal,
        )
        from driftpilot.signals.volume_spike_v1 import (
            VolumeSpikeConfig,
            VolumeSpikeV1Signal,
        )
        from driftpilot.signals.filing_8a_v1 import (
            Filing8AConfig,
            Filing8ASignal,
        )

        if catalyst_bus is None:
            raise RuntimeError(
                "--paper-live requires CATALYST_ENABLED=true (catalyst signals are "
                "the only ones wired for live order submission today)"
            )

        scanner: Any
        broker_for_machine: Any
        allocator_service: Any
        monitor_service: Any
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
        # Build signal config from runtime config (hot-reloadable from /admin)
        # falling back to .env defaults.
        from driftpilot.runtime_config import load_runtime_config
        rcfg = load_runtime_config()
        require_sent = rcfg.earnings_require_sentiment
        # Operator dispatches on active_signal. Order of precedence:
        # 1) runtime_config.json (UI-set, persistent) — only if the key is
        #    EXPLICITLY present (we don't let the dataclass default override env)
        # 2) ACTIVE_SIGNAL env var (settings.active_signal)
        import json as _json
        from pathlib import Path as _Path
        _rc_path = _Path("data/driftpilot/runtime_config.json")
        _rc_raw: dict = {}
        if _rc_path.exists():
            try:
                _rc_raw = _json.loads(_rc_path.read_text())
            except Exception:
                _rc_raw = {}
        active_signal_name = _rc_raw.get("active_signal") or settings.active_signal
        # Comma-separated list = run multiple signals in parallel via MultiSignal.
        # e.g. ACTIVE_SIGNAL="earnings_report_v1,filing_8a_v1"
        signal_names = [s.strip() for s in str(active_signal_name).split(",") if s.strip()]

        def _build_signal(name: str):
            if name == "analyst_target_raise_v1":
                logger.warning(
                    "🟠 %s — backtest verdict FAIL (edge_ratio=0.85). "
                    "Trading at known-negative expected value. "
                    "Qwen sentiment gate=%r active.", name, require_sent,
                )
                _analyst_sent = None if require_sent == "any" else require_sent
                return AnalystTargetRaiseV1Signal(
                    AnalystTargetRaiseConfig(
                        max_hold_minutes=rcfg.earnings_max_hold_minutes,
                        profit_take_pct=0.8,   # locked spec from catalyst_horizons
                        stop_loss_pct=1.0,     # locked spec from catalyst_horizons
                        max_event_age_minutes=rcfg.earnings_max_event_age_minutes,
                        require_sentiment=_analyst_sent,
                    ),
                    catalyst_bus,
                )
            if name == "volume_spike_v1":
                # Read universe for snapshot scanning
                _vol_symbols: list[str] = []
                with open(settings.universe_file) as _uf:
                    next(_uf, None)
                    for _line in _uf:
                        _s = _line.split(",", 1)[0].strip()
                        if _s:
                            _vol_symbols.append(_s)
                return VolumeSpikeV1Signal(
                    VolumeSpikeConfig(
                        max_hold_minutes=rcfg.earnings_max_hold_minutes,
                    ),
                    api_key=settings.alpaca_key_id,
                    api_secret=settings.alpaca_secret_key,
                    symbols=_vol_symbols,
                )
            if name == "filing_8a_v1":
                return Filing8ASignal(
                    Filing8AConfig(
                        max_hold_minutes=rcfg.earnings_max_hold_minutes,
                        profit_take_pct=rcfg.earnings_profit_take_pct,
                        stop_loss_pct=rcfg.earnings_stop_loss_pct,
                        max_event_age_minutes=rcfg.earnings_max_event_age_minutes,
                        trailing_enabled=str(rcfg.earnings_trailing_enabled).lower() == "true",
                        trailing_activation_pct=rcfg.earnings_trailing_activation_pct,
                        trailing_distance_pct=rcfg.earnings_trailing_distance_pct,
                    ),
                    catalyst_bus,
                )
            # Default: earnings_report_v1
            return EarningsReportSignal(
                EarningsReportConfig(
                    max_hold_minutes=rcfg.earnings_max_hold_minutes,
                    profit_take_pct=rcfg.earnings_profit_take_pct,
                    stop_loss_pct=rcfg.earnings_stop_loss_pct,
                    max_event_age_minutes=rcfg.earnings_max_event_age_minutes,
                    require_sentiment=None if require_sent == "any" else require_sent,
                    trailing_enabled=str(rcfg.earnings_trailing_enabled).lower() == "true",
                    trailing_activation_pct=rcfg.earnings_trailing_activation_pct,
                    trailing_distance_pct=rcfg.earnings_trailing_distance_pct,
                ),
                catalyst_bus,
            )

        sub_signals: list[Any] = [_build_signal(n) for n in signal_names]
        if len(sub_signals) > 1:
            live_signal = MultiSignal(sub_signals)
            logger.info("MULTI-SIGNAL active: %s", ", ".join(signal_names))
        else:
            live_signal = sub_signals[0]
            logger.info("single signal active: %s", signal_names[0])
        # subscribe() is async on most signals; sync/already-done on others.
        if hasattr(live_signal, "subscribe"):
            maybe = live_signal.subscribe()
            if hasattr(maybe, "__await__"):
                await maybe
        logger.info(
            "live signal config: max_hold=%dm profit_take=%.2f%% stop_loss=%.2f%% "
            "max_age=%dm require_sentiment=%s (hot-reloadable from /admin)",
            rcfg.earnings_max_hold_minutes, rcfg.earnings_profit_take_pct,
            rcfg.earnings_stop_loss_pct, rcfg.earnings_max_event_age_minutes,
            require_sent,
        )
        # Bootstrap _active_events from DB so events that landed before this
        # process started (or were just reclassified) are visible to the
        # signal without waiting for re-publication on the bus. MultiSignal
        # delegates to each sub-signal's bootstrap_from_db.
        if catalyst_db_path and hasattr(live_signal, "bootstrap_from_db"):
            n_loaded = live_signal.bootstrap_from_db(catalyst_db_path)
            logger.info("live signal bootstrapped %d events from DB", n_loaded)

        scanner = CatalystScannerService(
            signal=live_signal,
            quote_provider=allocator_service.broker.quote_provider,
            clock=clock,
            universe_path=settings.universe_file,
            runtime_config_path="data/driftpilot/runtime_config.json",
            repository=repository,
        )
        # Inject the same signal instance into the monitor so it skips the registry
        monitor_service._signal = live_signal
        logger.warning(
            "🚨 PAPER-LIVE MODE: submitting real orders to Alpaca paper account at %s",
            settings.alpaca_paper_base_url,
        )
        logger.info(
            "live signal: %s; scanner: CatalystScannerService (event-driven)",
            active_signal_name,
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

    # Build market-data adapter for agent snapshots.
    # In paper-live mode the REST quote provider has latest_quote but no
    # session bars — the adapter handles None bar_provider gracefully.
    market_adapter = MarketDataAdapter(
        bar_provider=None,  # TODO: wire AlpacaSIPStream when bar data is needed
        catalyst_db_path=catalyst_db_path,
    )

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
        orchestrator=orchestrator,
        market_adapter=market_adapter,
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
        orchestrator.stop()
        print(f"state={state.value} sqlite={settings.sqlite_path}")
        return

    # Run discovery service alongside the state machine so a feed crash
    # cannot kill the operator (DiscoveryService supervises with restarts).
    coros = [machine.run_forever()]
    if discovery_service is not None:
        coros.append(discovery_service.start())

    try:
        await asyncio.gather(*coros)
    finally:
        orchestrator.stop()


if __name__ == "__main__":
    main()
