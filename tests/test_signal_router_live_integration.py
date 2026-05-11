"""Integration tests: router wired into CatalystScannerService.

Tests the _annotate_with_routing path without hitting the broker or
market data. Uses a minimal stub scanner with injected candidates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from driftpilot.execution.slot_allocator import AllocationCandidate
from driftpilot.services_live import _RoutingEventStub
from driftpilot.signal_router import (
    EARNINGS_REPORT_V1,
    FILING_8A_V1,
    RoutingAction,
    RuleBasedRouter,
)

ET = ZoneInfo("America/New_York")


def _make_candidate(
    symbol: str = "AAPL",
    score: float = 0.5,
    category: str = "earnings",
    subcategory: str = "report",
    sentiment: str | None = "positive",
    priority_modifier: float = 0.1,
) -> AllocationCandidate:
    return AllocationCandidate(
        symbol=symbol,
        score=score,
        sector="Technology",
        latest_bar_at=datetime.now(timezone.utc),
        rank=1,
        metadata={
            "reference_price": 150.0,
            "catalyst_event_ts": "2024-10-15T10:00:00+00:00",
            "headline": f"Test headline for {symbol}",
            "sentiment": sentiment,
            "priority_modifier": priority_modifier,
            "category": category,
            "subcategory": subcategory,
            "signal_name": "earnings_report_v1",
        },
    )


class TestRoutingEventStub:
    def test_stub_duck_types_for_router(self):
        """The stub must have the fields the router reads."""
        stub = _RoutingEventStub(
            category="earnings",
            subcategory="report",
            sentiment="positive",
            priority_modifier=0.1,
        )
        router = RuleBasedRouter()
        decisions = router.route(stub)
        assert len(decisions) >= 1
        assert decisions[0].action in (RoutingAction.ROUTE, RoutingAction.DEFERRED)


class TestAnnotateWithRouting:
    """Test the _annotate_with_routing method directly."""

    def _build_scanner_with_router(self):
        """Build a minimal scanner with just the router wired."""
        from driftpilot.services_live import CatalystScannerService

        scanner = CatalystScannerService.__new__(CatalystScannerService)
        scanner._router = RuleBasedRouter()
        return scanner

    def test_positive_earnings_passes_through(self):
        scanner = self._build_scanner_with_router()
        candidates = [_make_candidate(sentiment="positive")]
        now = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        result = scanner._annotate_with_routing(candidates, now)
        assert len(result) == 1
        meta = result[0].metadata
        assert "routing_decisions" in meta
        assert meta.get("routed_signal") == EARNINGS_REPORT_V1

    def test_negative_earnings_blocked(self):
        scanner = self._build_scanner_with_router()
        candidates = [_make_candidate(sentiment="negative")]
        now = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        result = scanner._annotate_with_routing(candidates, now)
        assert len(result) == 0  # blocked

    def test_target_cut_blocked(self):
        scanner = self._build_scanner_with_router()
        candidates = [_make_candidate(
            category="analyst", subcategory="target_cut",
            sentiment="negative",
        )]
        now = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        result = scanner._annotate_with_routing(candidates, now)
        assert len(result) == 0  # blocked

    def test_no_category_passes_through(self):
        """Candidates without category info should pass through unmodified."""
        scanner = self._build_scanner_with_router()
        candidate = AllocationCandidate(
            symbol="XYZ",
            score=0.5,
            sector="Unknown",
            latest_bar_at=datetime.now(timezone.utc),
            rank=1,
            metadata={"reference_price": 100.0},  # no category
        )
        now = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        result = scanner._annotate_with_routing([candidate], now)
        assert len(result) == 1
        assert result[0].symbol == "XYZ"

    def test_filing_8a_positive_routes(self):
        scanner = self._build_scanner_with_router()
        candidates = [_make_candidate(
            category="filing", subcategory="8a",
            sentiment="positive",
        )]
        now = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        result = scanner._annotate_with_routing(candidates, now)
        assert len(result) == 1
        assert result[0].metadata.get("routed_signal") == FILING_8A_V1

    def test_mixed_candidates_filter_blocks_only(self):
        scanner = self._build_scanner_with_router()
        candidates = [
            _make_candidate(symbol="GOOD", sentiment="positive"),
            _make_candidate(symbol="BAD", sentiment="negative"),
            _make_candidate(symbol="OK", category="filing", subcategory="8a", sentiment="neutral"),
        ]
        now = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        result = scanner._annotate_with_routing(candidates, now)
        symbols = [r.symbol for r in result]
        assert "GOOD" in symbols
        assert "BAD" not in symbols  # blocked
        assert "OK" in symbols

    def test_evaluate_exit_unchanged(self):
        """Router does NOT touch evaluate_exit — MultiSignal still routes by signal_name."""
        # This is a documentation test: verify the invariant holds
        from driftpilot.services_live import MultiSignal
        from unittest.mock import MagicMock

        sig1 = MagicMock()
        sig1.name = "earnings_report_v1"
        sig1.evaluate_exit.return_value = "hold"

        sig2 = MagicMock()
        sig2.name = "filing_8a_v1"
        sig2.evaluate_exit.return_value = "exit"

        ms = MultiSignal([sig1, sig2])

        pos_mock = MagicMock()
        pos_mock.metadata = {"signal_name": "filing_8a_v1"}
        result = ms.evaluate_exit(pos_mock, datetime.now(timezone.utc))
        assert result == "exit"
        sig2.evaluate_exit.assert_called_once()
