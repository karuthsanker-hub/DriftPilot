"""DriftPilot live catalyst observer (read-only).

Connects to live Alpaca News + Qwen enrichment + the catalyst bus. Every
N seconds, prints the active events the signal is tracking and the
candidates `signal.scan()` would emit. **Does not place orders.** Safe
to run during market hours; safe to leave running unattended.

Use this to validate the v3 catalyst pipeline against live market data
before wiring real broker execution.

Run:
  CATALYST_ENABLED=true python -m driftpilot.observer

The Alpaca news feed and Qwen enricher are constructed exactly as the
operator runtime would — same code path. The only difference is no
orders are submitted.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from driftpilot.catalyst.classifier import CatalystClassifier
from driftpilot.catalyst.db import init_catalyst_schema
from driftpilot.catalyst.discovery_service import DiscoveryService
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.catalyst.feed_alpaca import AlpacaNewsFeed
from driftpilot.catalyst.qwen_enricher import QwenEnricher
from driftpilot.catalyst.universe_filter import CatalystUniverseFilter
from driftpilot.settings import load_settings
from driftpilot.signals.analyst_target_raise_v1 import (
    AnalystTargetRaiseConfig,
    AnalystTargetRaiseV1Signal,
)
from driftpilot.signals.earnings_report_v1 import (
    EarningsReportConfig,
    EarningsReportSignal,
)


logger = logging.getLogger("observer")


def _load_universe(path: str) -> list[str]:
    out: list[str] = []
    with open(path) as f:
        next(f, None)
        for line in f:
            sym = line.split(",", 1)[0].strip()
            if sym:
                out.append(sym)
    return out


async def _print_state(signals: dict, universe_filter: CatalystUniverseFilter, now: datetime) -> None:
    """One status snapshot. Prints active events + candidates per signal."""
    line = f"\n=== {now.isoformat()} ==="
    print(line)

    import inspect
    for name, sig in signals.items():
        active = getattr(sig, "_active_events", {})
        # Two signal shapes: earnings_report_v1's scan() is async; target_raise's
        # scan() is sync. Call accordingly.
        try:
            res = sig.scan(now=now)
        except TypeError:
            res = sig.scan()
        candidates = await res if inspect.isawaitable(res) else res
        admitted_count = len(candidates)
        admitted_symbols = sorted({c.symbol for c in candidates})

        # Sentiment breakdown of active (subscribed) events
        sentiments: dict[str, int] = {}
        for ev in active.values():
            sentiments[ev.sentiment or "unenriched"] = sentiments.get(ev.sentiment or "unenriched", 0) + 1

        print(f"  signal: {name}")
        print(f"    active_subscribed_events: {len(active)}  sentiments: {sentiments}")
        print(f"    candidates_admitted: {admitted_count}")
        if admitted_symbols:
            print(f"    symbols: {', '.join(admitted_symbols[:10])}{' ...' if len(admitted_symbols) > 10 else ''}")
            for c in candidates[:5]:
                age = c.features.get("event_age_minutes", 0)
                head = (c.features.get("headline") or "")[:80]
                sent = c.features.get("sentiment")
                print(f"      [{c.symbol}] age={age:.1f}min sentiment={sent} score={c.score:+.2f}  {head}")


async def _periodic_status(signals: dict, universe_filter: CatalystUniverseFilter, every_s: int) -> None:
    while True:
        now = datetime.now(timezone.utc)
        try:
            await _print_state(signals, universe_filter, now)
        except Exception as exc:
            logger.exception("status snapshot failed: %s", exc)
        await asyncio.sleep(every_s)


async def main_async(args) -> None:
    settings = load_settings(args.env_file)

    if not settings.alpaca_key_id or not settings.alpaca_secret_key:
        logger.error("ALPACA credentials missing in %s — cannot run observer", args.env_file)
        sys.exit(2)

    # Build catalyst layer ourselves (same shape as operator._build_catalyst_layer)
    Path(settings.catalyst_db_path).parent.mkdir(parents=True, exist_ok=True)
    init_catalyst_schema(settings.catalyst_db_path)

    bus = CatalystEventBus()
    universe_filter = CatalystUniverseFilter(
        settings.catalyst_db_path,
        lookback_minutes=settings.catalyst_universe_lookback_minutes,
    )

    classifier = CatalystClassifier()
    enricher = QwenEnricher(
        base_url=settings.catalyst_qwen_url,
        model=settings.catalyst_qwen_model,
        timeout_ms=settings.catalyst_qwen_timeout_ms,
    )

    symbols = _load_universe(settings.universe_file)
    logger.info("universe: %d symbols", len(symbols))

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
    feeds: list[tuple[str, Callable[[], Awaitable[None]]]] = [("alpaca_news", alpaca_feed.run)]
    discovery = DiscoveryService(feeds)

    # Build the two catalyst signals (the validated GATED config: positive sentiment)
    earnings_sig = EarningsReportSignal(
        EarningsReportConfig(require_sentiment="positive"), bus
    )
    target_raise_sig = AnalystTargetRaiseV1Signal(AnalystTargetRaiseConfig(), bus)

    # earnings_report_v1 has explicit subscribe(); target_raise_v1 subscribes
    # in __init__ via asyncio.create_task. Both shapes are supported here.
    if hasattr(earnings_sig, "subscribe"):
        await earnings_sig.subscribe()

    signals = {
        "earnings_report_v1": earnings_sig,
        "analyst_target_raise_v1": target_raise_sig,
    }

    logger.info("=" * 70)
    logger.info("DriftPilot LIVE OBSERVER — read-only, NO orders will be submitted")
    logger.info("Alpaca news poll: every %ds  Qwen: %s  status print: every %ds",
                settings.catalyst_alpaca_poll_seconds, settings.catalyst_qwen_url, args.print_every_s)
    logger.info("=" * 70)

    # Run discovery + status loop concurrently
    tasks = [
        asyncio.create_task(discovery.start()),
        asyncio.create_task(_periodic_status(signals, universe_filter, args.print_every_s)),
    ]

    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("shutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if hasattr(earnings_sig, "unsubscribe"):
            try:
                await earnings_sig.unsubscribe()
            except Exception:
                pass


def main() -> None:
    p = argparse.ArgumentParser(description="DriftPilot live catalyst observer (read-only)")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--print-every-s", type=int, default=60,
                   help="Status snapshot cadence (seconds). Default 60.")
    args = p.parse_args()

    logging.basicConfig(
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    # Quiet down httpx — Qwen calls are noisy
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted")


if __name__ == "__main__":
    main()
