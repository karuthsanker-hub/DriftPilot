"""Mid-price limit-order fill simulation for the backtest harness.

This module simulates whether a resting mid-price limit order would have been
filled by subsequent real price action. It is introduced by Phase 3 Task 3.3 of
the DriftPilot Locked Integration Refactor Plan v1.1 to support signals (e.g.
``rs_drift_v1``) that mark ``ENTRY_ORDER_TYPE = "limit_mid"``.

WHY THE PLACEMENT-TIME QUOTE MATTERS
------------------------------------
An earlier draft of this code computed the "mid" as ``(bar.high + bar.low) / 2``
for the SAME bar that was being checked, then asked whether that mid lay between
``bar.low`` and ``bar.high``. That comparison is tautologically true: the
mid-of-a-bar is ALWAYS inside the bar's range, so every order would "fill",
producing a fictitious 100% fill rate that flatters every strategy and corrupts
backtest expectancy.

The correct semantics are:

1. The limit price is fixed at the moment of placement (``placement_mid_price``)
   based on the bid/ask at that instant.
2. We then look at SUBSEQUENT bars (bars whose ``timestamp`` is strictly after
   ``order.placed_at``) and ask whether actual price action traded down to (for
   a buy) or up through (for a sell) that fixed limit price.

The bar's own midpoint is irrelevant. Only ``bar.low`` (for buys) and
``bar.high`` (for sells) matter, because those represent the real intraday
extremes that the order book actually traded at.

This module is intentionally scoped narrowly: it adds the simulation primitive
plus exhaustive tests. Integration into the per-bar loop in
``driftpilot.backtest.replay`` is deferred to a later phase of the refactor
plan, and is therefore NOT yet wired into ``replay.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Literal

from driftpilot.clock import require_aware
from driftpilot.signals.features import MinuteBar


Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class LimitOrder:
    """A resting mid-price limit order placed at a specific moment.

    Critical: the limit price is the placement-time mid, NOT a bar-derived mid.
    """

    symbol: str
    placed_at: datetime
    placement_mid_price: float
    placement_bid: float
    placement_ask: float
    timeout_seconds: int
    side: Side

    def __post_init__(self) -> None:
        require_aware(self.placed_at)
        if self.placement_mid_price <= 0:
            raise ValueError("placement_mid_price must be positive")
        if self.placement_bid <= 0 or self.placement_ask <= 0:
            raise ValueError("placement bid/ask must be positive")
        if self.placement_ask < self.placement_bid:
            raise ValueError("placement ask must be >= placement bid")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")


@dataclass(frozen=True, slots=True)
class LimitFillResult:
    filled: bool
    price: float | None = None
    filled_at: datetime | None = None
    reason: str = ""  # "filled" | "timeout" | "no_subsequent_bars"


def attempt_limit_fill(
    order: LimitOrder, subsequent_bars: Iterable[MinuteBar]
) -> LimitFillResult:
    """Determine if a resting mid-price limit would have filled within the
    timeout window.

    For a buy limit at ``placement_mid_price``: fill iff a subsequent bar's
    ``low`` touched or crossed the limit (real price action traded down to or
    through it).
    For a sell limit: fill iff a subsequent bar's ``high`` touched or crossed.

    The bar's own midpoint is IRRELEVANT — we need actual price movement.

    Bars whose timestamp is at or before ``order.placed_at`` are skipped (an
    order cannot be filled by the bar that contained the placement decision).
    Bars whose timestamp exceeds ``order.placed_at + timeout_seconds`` cause the
    function to return a ``timeout`` result without considering them.
    """

    deadline = order.placed_at + timedelta(seconds=order.timeout_seconds)
    saw_any_candidate_bar = False

    for bar in subsequent_bars:
        if bar.symbol.upper() != order.symbol.upper():
            # Defensive: fill simulation must operate on the order's symbol.
            continue
        if bar.timestamp <= order.placed_at:
            continue
        if bar.timestamp > deadline:
            if saw_any_candidate_bar:
                return LimitFillResult(filled=False, reason="timeout")
            return LimitFillResult(filled=False, reason="timeout")

        saw_any_candidate_bar = True

        if order.side == "buy":
            if bar.low <= order.placement_mid_price:
                return LimitFillResult(
                    filled=True,
                    price=order.placement_mid_price,
                    filled_at=bar.timestamp,
                    reason="filled",
                )
        else:  # sell
            if bar.high >= order.placement_mid_price:
                return LimitFillResult(
                    filled=True,
                    price=order.placement_mid_price,
                    filled_at=bar.timestamp,
                    reason="filled",
                )

    if not saw_any_candidate_bar:
        return LimitFillResult(filled=False, reason="no_subsequent_bars")
    return LimitFillResult(filled=False, reason="timeout")
