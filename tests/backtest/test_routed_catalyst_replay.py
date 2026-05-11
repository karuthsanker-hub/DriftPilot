"""Smoke tests: router decisions on fabricated catalyst events.

Validates that the router correctly maps events to signals in the
context of a backtest replay. Does NOT run the full Jul-Dec 2024
comparison (that requires Phase 3 technical-signal integration).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from driftpilot.signal_router import (
    EARNINGS_REPORT_V1,
    FILING_8A_V1,
    RoutingAction,
    RuleBasedRouter,
)

ET = ZoneInfo("America/New_York")


@dataclass
class _Evt:
    category: str
    subcategory: str
    sentiment: str | None = None
    priority_modifier: float = 0.0


class TestRoutedCatalystReplaySmoke:
    """Fabricated events → expected routing decisions."""

    def test_earnings_report_positive_routes_to_earnings_v1(self):
        """An earnings/report positive event should route to earnings_report_v1."""
        router = RuleBasedRouter()
        evt = _Evt("earnings", "report", "positive", 0.15)
        t = datetime(2024, 10, 15, 10, 30, tzinfo=ET)
        decisions = router.route(evt, regime="GREEN", time_et=t)

        routes = [d for d in decisions if d.action == RoutingAction.ROUTE]
        assert len(routes) >= 1
        assert routes[0].signal_name == EARNINGS_REPORT_V1
        assert routes[0].horizon_minutes == 60
        assert routes[0].conviction > 0.5

    def test_target_cut_blocks_no_routed_trade(self):
        """A target_cut event should BLOCK — no trade should be routed."""
        router = RuleBasedRouter()
        evt = _Evt("analyst", "target_cut", "negative", -0.1)
        t = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        decisions = router.route(evt, regime="GREEN", time_et=t)

        routes = [d for d in decisions if d.action == RoutingAction.ROUTE]
        assert len(routes) == 0
        blocks = [d for d in decisions if d.action == RoutingAction.BLOCK]
        assert len(blocks) == 1
        assert blocks[0].horizon_minutes == 240

    def test_filing_8a_positive_routes_to_filing_v1(self):
        """A filing/8a positive event should route to filing_8a_v1."""
        router = RuleBasedRouter()
        evt = _Evt("filing", "8a", "positive", 0.1)
        t = datetime(2024, 10, 15, 10, 30, tzinfo=ET)
        decisions = router.route(evt, regime="GREEN", time_et=t)

        routes = [d for d in decisions if d.action == RoutingAction.ROUTE]
        assert len(routes) >= 1
        assert routes[0].signal_name == FILING_8A_V1
        assert routes[0].horizon_minutes == 60

    def test_filing_8a_neutral_routes_lower_conviction(self):
        """Neutral filing/8a should still route but with lower conviction."""
        router = RuleBasedRouter()
        pos = _Evt("filing", "8a", "positive", 0.1)
        neu = _Evt("filing", "8a", "neutral", 0.0)
        t = datetime(2024, 10, 15, 10, 30, tzinfo=ET)

        pos_decisions = router.route(pos, time_et=t)
        neu_decisions = router.route(neu, time_et=t)

        pos_routes = [d for d in pos_decisions if d.action == RoutingAction.ROUTE]
        neu_routes = [d for d in neu_decisions if d.action == RoutingAction.ROUTE]

        assert pos_routes[0].conviction > neu_routes[0].conviction

    def test_negative_earnings_blocks_long(self):
        """Negative earnings should BLOCK — no long trade routed."""
        router = RuleBasedRouter()
        evt = _Evt("earnings", "report", "negative", -0.15)
        t = datetime(2024, 10, 15, 10, 30, tzinfo=ET)
        decisions = router.route(evt, regime="GREEN", time_et=t)

        routes = [d for d in decisions if d.action == RoutingAction.ROUTE]
        blocks = [d for d in decisions if d.action == RoutingAction.BLOCK]
        assert len(routes) == 0
        assert len(blocks) == 1

    def test_bulk_routing_distribution(self):
        """Route a mix of events and verify the distribution makes sense."""
        router = RuleBasedRouter()
        events = [
            _Evt("earnings", "report", "positive", 0.1),
            _Evt("earnings", "report", "positive", 0.05),
            _Evt("earnings", "report", "negative", -0.1),
            _Evt("earnings", "report", "neutral", 0.0),
            _Evt("filing", "8a", "positive", 0.1),
            _Evt("filing", "8a", "neutral", 0.0),
            _Evt("filing", "8a", "negative", -0.05),
            _Evt("analyst", "target_cut", "negative", -0.15),
            _Evt("analyst", "target_raise", "positive", 0.1),
            _Evt("analyst", "downgrade", "negative", -0.1),
        ]
        t = datetime(2024, 10, 15, 11, 0, tzinfo=ET)

        actions = {}
        for evt in events:
            decisions = router.route(evt, regime="GREEN", time_et=t)
            key = f"{evt.category}/{evt.subcategory}/{evt.sentiment}"
            actions[key] = [d.action for d in decisions]

        # Verify: positive earnings → ROUTE
        assert RoutingAction.ROUTE in actions["earnings/report/positive"]
        # Verify: negative earnings → BLOCK
        assert RoutingAction.BLOCK in actions["earnings/report/negative"]
        # Verify: positive filing → ROUTE
        assert RoutingAction.ROUTE in actions["filing/8a/positive"]
        # Verify: negative filing → BLOCK
        assert RoutingAction.BLOCK in actions["filing/8a/negative"]
        # Verify: target_cut → BLOCK
        assert RoutingAction.BLOCK in actions["analyst/target_cut/negative"]
        # Verify: downgrade → BLOCK
        assert RoutingAction.BLOCK in actions["analyst/downgrade/negative"]

    def test_exit_only_window_blocks_all(self):
        """During exit-only window, even positive earnings should not route."""
        router = RuleBasedRouter()
        evt = _Evt("earnings", "report", "positive", 0.15)
        t = datetime(2024, 10, 15, 15, 50, tzinfo=ET)
        decisions = router.route(evt, regime="GREEN", time_et=t)

        routes = [d for d in decisions if d.action == RoutingAction.ROUTE]
        assert len(routes) == 0

    def test_red_regime_catalyst_only(self):
        """In RED regime, only catalyst signals should route."""
        router = RuleBasedRouter(enable_technical_signals=True)
        evt = _Evt("earnings", "report", "positive", 0.1)
        t = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        decisions = router.route(evt, regime="RED", time_et=t)

        for d in decisions:
            if d.action == RoutingAction.ROUTE:
                assert d.signal_name in (EARNINGS_REPORT_V1, FILING_8A_V1)
