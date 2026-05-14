"""Configuration for volume_spike_v1 signal.

Detects unusual intraday volume relative to average daily volume.
Stocks with volume spikes often move directionally — smart money
moves before headlines drop.
"""

from __future__ import annotations

from dataclasses import dataclass


SIGNAL_NAME = "volume_spike_v1"
SIGNAL_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class VolumeSpikeConfig:
    """Tunables for the volume spike scanner."""

    # Minimum volume ratio (today's volume / avg daily volume) to trigger.
    # At 10:00 AM you'd expect ~15% of daily volume done, so a 2x ratio
    # at that point means the stock is trading at 13x normal pace.
    min_volume_ratio: float = 1.5

    # Minimum absolute volume today to avoid illiquid penny stocks.
    min_volume_abs: int = 500_000

    # Minimum price to filter out penny stocks.
    min_price: float = 5.0

    # Maximum price to stay in the mid-cap/small-cap sweet spot
    # (0 = no cap).
    max_price: float = 0.0

    # Minimum intraday price change % to confirm direction.
    # We want volume + movement, not just high-volume chop.
    min_price_change_pct: float = 0.5

    # Maximum number of candidates to emit per scan.
    max_candidates: int = 20

    # How often to poll Alpaca snapshots (seconds). Scanner calls scan()
    # on its own cadence, but internal cache avoids redundant API calls.
    snapshot_cache_ttl_s: float = 30.0

    # Max hold time (minutes) for positions from this signal.
    max_hold_minutes: int = 60

    # Exit thresholds.
    profit_take_pct: float = 1.5
    stop_loss_pct: float = 1.0


__all__ = ["SIGNAL_NAME", "SIGNAL_VERSION", "VolumeSpikeConfig"]
