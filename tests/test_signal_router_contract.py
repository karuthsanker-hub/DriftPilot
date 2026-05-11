"""Contract tests for the signal router — invariants that any router impl must satisfy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signal_router import (
    ALL_KNOWN_SIGNALS,
    CATALYST_SAFE_SIGNALS,
    TECHNICAL_SIGNALS,
    RoutingAction,
    RoutingDecision,
    RuleBasedRouter,
)

ET = ZoneInfo("America/New_York")


@dataclass
class _Evt:
    category: str
    subcategory: str
    sentiment: str | None = None
    priority_modifier: float = 0.0


# All catalyst categories from the thesis matrix + some unknowns
_MATRIX_EVENTS = [
    _Evt("earnings", "report", "positive", 0.1),
    _Evt("earnings", "report", "negative", -0.1),
    _Evt("earnings", "report", "neutral"),
    _Evt("earnings", "report", None),
    _Evt("earnings", "guidance_up", "positive"),
    _Evt("earnings", "guidance_down", "negative"),
    _Evt("earnings", "beat", "positive", 0.15),
    _Evt("earnings", "miss", "negative", -0.15),
    _Evt("analyst", "target_raise", "positive"),
    _Evt("analyst", "target_raise", "neutral"),
    _Evt("analyst", "target_cut", "negative"),
    _Evt("analyst", "upgrade", "positive"),
    _Evt("analyst", "downgrade", "negative"),
    _Evt("filing", "8a", "positive"),
    _Evt("filing", "8a", "neutral"),
    _Evt("filing", "8a", "negative"),
    _Evt("m_and_a", "acquires", "positive"),
    _Evt("m_and_a", "merger", "positive"),
    _Evt("product", "launch", "positive"),  # unknown
    _Evt("legal", "lawsuit", "negative"),   # unknown
]


class TestContractInvariants:
    """Invariants that must hold for every possible event input."""

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_route_returns_non_empty_list(self, event: _Evt):
        r = RuleBasedRouter()
        ds = r.route(event)
        assert isinstance(ds, list)
        assert len(ds) >= 1

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_every_decision_has_valid_action(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            assert isinstance(d.action, RoutingAction)

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_route_decisions_only_reference_known_signals(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            if d.signal_name is not None:
                assert d.signal_name in ALL_KNOWN_SIGNALS, (
                    f"Unknown signal {d.signal_name} in decision for {event.category}/{event.subcategory}"
                )

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_conviction_bounded(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            assert 0.0 <= d.conviction <= 1.0

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_horizon_non_negative(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            assert d.horizon_minutes >= 0

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_every_decision_has_rule_id_and_reason(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            assert d.rule_id, f"missing rule_id for {event}"
            assert d.reason, f"missing reason for {event}"

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_block_decisions_have_no_signal(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            if d.action == RoutingAction.BLOCK:
                assert d.signal_name is None
                assert d.horizon_minutes > 0, "BLOCK must have a positive TTL"

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_skip_decisions_have_no_signal(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            if d.action == RoutingAction.SKIP:
                assert d.signal_name is None

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_route_decisions_have_signal(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            if d.action == RoutingAction.ROUTE:
                assert d.signal_name is not None

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_decision_echoes_event_fields(self, event: _Evt):
        r = RuleBasedRouter()
        for d in r.route(event):
            assert d.category == event.category
            assert d.subcategory == event.subcategory
            assert d.sentiment == event.sentiment


class TestNaiveDatetimeFails:
    def test_naive_datetime_raises(self):
        r = RuleBasedRouter()
        naive = datetime(2024, 10, 15, 11, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            r.route(_Evt("earnings", "report", "positive"), time_et=naive)

    def test_aware_datetime_works(self):
        r = RuleBasedRouter()
        aware = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        ds = r.route(_Evt("earnings", "report", "positive"), time_et=aware)
        assert len(ds) >= 1


class TestDeterminism:
    """Same input → same output, always."""

    def test_identical_calls_produce_identical_results(self):
        r = RuleBasedRouter()
        evt = _Evt("earnings", "report", "positive", 0.1)
        t = datetime(2024, 10, 15, 11, 0, tzinfo=ET)
        ds1 = r.route(evt, regime="GREEN", time_et=t)
        ds2 = r.route(evt, regime="GREEN", time_et=t)
        assert len(ds1) == len(ds2)
        for a, b in zip(ds1, ds2):
            assert a.action == b.action
            assert a.signal_name == b.signal_name
            assert a.conviction == b.conviction
            assert a.rule_id == b.rule_id


class TestDefaultModeNoTechnicalRoutes:
    """With enable_technical_signals=False (default), no ROUTE to technical signals."""

    @pytest.mark.parametrize("event", _MATRIX_EVENTS, ids=lambda e: f"{e.category}/{e.subcategory}/{e.sentiment}")
    def test_no_technical_routes_by_default(self, event: _Evt):
        r = RuleBasedRouter()  # default: enable_technical_signals=False
        for d in r.route(event):
            if d.action == RoutingAction.ROUTE:
                assert d.signal_name in CATALYST_SAFE_SIGNALS, (
                    f"Default mode should not ROUTE to technical signal {d.signal_name}"
                )


class TestRoutingDecisionValidation:
    def test_route_without_signal_raises(self):
        with pytest.raises(ValueError, match="signal_name"):
            RoutingDecision(
                action=RoutingAction.ROUTE,
                signal_name=None,
                horizon_minutes=60,
                conviction=0.8,
                rule_id="test",
                reason="test",
                category="earnings",
                subcategory="report",
                sentiment="positive",
                priority_modifier=0.0,
            )

    def test_route_with_unknown_signal_raises(self):
        with pytest.raises(ValueError, match="unknown signal"):
            RoutingDecision(
                action=RoutingAction.ROUTE,
                signal_name="fake_signal_v99",
                horizon_minutes=60,
                conviction=0.8,
                rule_id="test",
                reason="test",
                category="earnings",
                subcategory="report",
                sentiment="positive",
                priority_modifier=0.0,
            )

    def test_block_with_none_signal_ok(self):
        d = RoutingDecision(
            action=RoutingAction.BLOCK,
            signal_name=None,
            horizon_minutes=240,
            conviction=0.0,
            rule_id="test",
            reason="test block",
            category="analyst",
            subcategory="target_cut",
            sentiment="negative",
            priority_modifier=-0.1,
        )
        assert d.action == RoutingAction.BLOCK


class TestSignalSetsConsistent:
    def test_catalyst_and_technical_disjoint(self):
        assert CATALYST_SAFE_SIGNALS & TECHNICAL_SIGNALS == frozenset()

    def test_all_known_is_union(self):
        assert ALL_KNOWN_SIGNALS == CATALYST_SAFE_SIGNALS | TECHNICAL_SIGNALS
