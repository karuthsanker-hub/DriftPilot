"""Tests for RuleBasedRouter — thesis matrix rule coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from driftpilot.signal_router import (
    APEX_HUNTER_V2_2,
    CATALYST_SAFE_SIGNALS,
    EARNINGS_REPORT_V1,
    FILING_8A_V1,
    RS_DRIFT_V1,
    STATIONARY_GHOST_V1,
    WHALE_TAIL_V1,
    RoutingAction,
    RoutingDecision,
    RuleBasedRouter,
)

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Stub event — duck-typed for the router
# ---------------------------------------------------------------------------

@dataclass
class _Evt:
    category: str
    subcategory: str
    sentiment: str | None = None
    priority_modifier: float = 0.0


def _et(hour: int, minute: int = 0) -> datetime:
    """Create an aware ET datetime for testing time filters."""
    return datetime(2024, 10, 15, hour, minute, tzinfo=ET)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _route_actions(decisions: list[RoutingDecision]) -> list[RoutingAction]:
    return [d.action for d in decisions]


def _route_signals(decisions: list[RoutingDecision]) -> list[str | None]:
    return [d.signal_name for d in decisions]


# ---------------------------------------------------------------------------
# Earnings / report
# ---------------------------------------------------------------------------

class TestEarningsReport:
    def test_positive_routes_to_earnings_primary(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "report", "positive", 0.1))
        routes = [d for d in ds if d.action == RoutingAction.ROUTE]
        assert len(routes) >= 1
        assert routes[0].signal_name == EARNINGS_REPORT_V1
        assert routes[0].horizon_minutes == 60
        assert routes[0].conviction >= 0.7

    def test_positive_has_whale_tail_secondary(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "report", "positive"))
        secondaries = [d for d in ds if d.signal_name == WHALE_TAIL_V1]
        assert len(secondaries) == 1
        # Technical signal is DEFERRED by default
        assert secondaries[0].action == RoutingAction.DEFERRED

    def test_positive_whale_tail_routes_when_enabled(self):
        r = RuleBasedRouter(enable_technical_signals=True)
        ds = r.route(_Evt("earnings", "report", "positive"))
        wt = [d for d in ds if d.signal_name == WHALE_TAIL_V1]
        assert len(wt) == 1
        assert wt[0].action == RoutingAction.ROUTE

    def test_negative_blocks(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "report", "negative"))
        assert len(ds) == 1
        assert ds[0].action == RoutingAction.BLOCK
        assert ds[0].horizon_minutes == 240
        assert "anti-signal" in ds[0].reason.lower() or "block" in ds[0].reason.lower()

    def test_neutral_routes_lower_conviction(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "report", "neutral"))
        routes = [d for d in ds if d.action == RoutingAction.ROUTE]
        assert len(routes) == 1
        assert routes[0].signal_name == EARNINGS_REPORT_V1
        assert routes[0].conviction < 0.7

    def test_none_sentiment_routes_lower_conviction(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "report", None))
        routes = [d for d in ds if d.action == RoutingAction.ROUTE]
        assert len(routes) == 1
        assert routes[0].signal_name == EARNINGS_REPORT_V1


# ---------------------------------------------------------------------------
# Earnings / guidance
# ---------------------------------------------------------------------------

class TestEarningsGuidance:
    def test_guidance_up_positive_routes_to_earnings(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "guidance_up", "positive"))
        routes = [d for d in ds if d.action == RoutingAction.ROUTE]
        assert routes[0].signal_name == EARNINGS_REPORT_V1

    def test_guidance_down_blocks(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "guidance_down", "negative"))
        assert ds[0].action == RoutingAction.BLOCK
        assert ds[0].horizon_minutes == 240

    def test_earnings_beat_routes_high_conviction(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "beat", "positive", 0.15))
        routes = [d for d in ds if d.action == RoutingAction.ROUTE]
        assert routes[0].signal_name == EARNINGS_REPORT_V1
        assert routes[0].conviction >= 0.8

    def test_earnings_miss_blocks(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "miss", "negative"))
        assert ds[0].action == RoutingAction.BLOCK


# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------

class TestAnalyst:
    def test_target_raise_positive_routes_to_apex(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("analyst", "target_raise", "positive"))
        # Apex is technical — DEFERRED by default
        assert ds[0].action == RoutingAction.DEFERRED
        assert ds[0].signal_name == APEX_HUNTER_V2_2

    def test_target_raise_neutral_skips(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("analyst", "target_raise", "neutral"))
        assert ds[0].action == RoutingAction.SKIP
        assert "no edge" in ds[0].reason.lower() or "unsure" in ds[0].reason.lower()

    def test_target_cut_blocks_any_sentiment(self):
        for sentiment in ("positive", "negative", "neutral", None):
            r = RuleBasedRouter()
            ds = r.route(_Evt("analyst", "target_cut", sentiment))
            assert ds[0].action == RoutingAction.BLOCK, f"failed for sentiment={sentiment}"
            assert ds[0].horizon_minutes == 240

    def test_upgrade_positive_routes_to_apex(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("analyst", "upgrade", "positive"))
        assert ds[0].signal_name == APEX_HUNTER_V2_2

    def test_downgrade_blocks(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("analyst", "downgrade", "negative"))
        assert ds[0].action == RoutingAction.BLOCK
        assert ds[0].horizon_minutes == 1440  # 1 day


# ---------------------------------------------------------------------------
# Filing / 8a
# ---------------------------------------------------------------------------

class TestFiling8A:
    def test_positive_routes_to_filing_primary(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("filing", "8a", "positive"))
        routes = [d for d in ds if d.action == RoutingAction.ROUTE]
        assert len(routes) >= 1
        assert routes[0].signal_name == FILING_8A_V1
        assert routes[0].conviction >= 0.5

    def test_positive_has_ghost_secondary(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("filing", "8a", "positive"))
        ghosts = [d for d in ds if d.signal_name == STATIONARY_GHOST_V1]
        assert len(ghosts) == 1
        assert ghosts[0].action == RoutingAction.DEFERRED

    def test_neutral_routes_lower_conviction(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("filing", "8a", "neutral"))
        routes = [d for d in ds if d.action == RoutingAction.ROUTE]
        assert len(routes) == 1
        assert routes[0].signal_name == FILING_8A_V1
        assert routes[0].conviction < 0.5

    def test_negative_blocks(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("filing", "8a", "negative"))
        assert ds[0].action == RoutingAction.BLOCK


# ---------------------------------------------------------------------------
# M&A
# ---------------------------------------------------------------------------

class TestMAndA:
    def test_acquires_positive_routes_to_rs_drift(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("m_and_a", "acquires", "positive"))
        assert ds[0].signal_name == RS_DRIFT_V1
        assert ds[0].horizon_minutes == 1440  # multi-day

    def test_acquires_non_positive_skips(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("m_and_a", "acquires", "neutral"))
        assert ds[0].action == RoutingAction.SKIP

    def test_merger_routes_same_as_acquires(self):
        r = RuleBasedRouter()
        ds_a = r.route(_Evt("m_and_a", "acquires", "positive"))
        ds_m = r.route(_Evt("m_and_a", "merger", "positive"))
        assert ds_a[0].signal_name == ds_m[0].signal_name


# ---------------------------------------------------------------------------
# Unknown categories
# ---------------------------------------------------------------------------

class TestUnknown:
    def test_unknown_category_skips(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("product", "launch", "positive"))
        assert ds[0].action == RoutingAction.SKIP
        assert "no routing rule" in ds[0].reason.lower() or "no rule" in ds[0].reason.lower()

    def test_unknown_subcategory_skips(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "special_dividend", "positive"))
        assert ds[0].action == RoutingAction.SKIP


# ---------------------------------------------------------------------------
# Regime filters
# ---------------------------------------------------------------------------

class TestRegimeFilter:
    def test_red_regime_blocks_technical_signals(self):
        r = RuleBasedRouter(enable_technical_signals=True)
        # Positive earnings routes to earnings_report_v1 + whale_tail_v1
        ds = r.route(
            _Evt("earnings", "report", "positive"),
            regime="RED",
            time_et=_et(11, 0),
        )
        for d in ds:
            if d.action == RoutingAction.ROUTE:
                assert d.signal_name in CATALYST_SAFE_SIGNALS, (
                    f"RED regime should block {d.signal_name}"
                )

    def test_green_regime_allows_all(self):
        r = RuleBasedRouter(enable_technical_signals=True)
        ds = r.route(
            _Evt("earnings", "report", "positive"),
            regime="GREEN",
            time_et=_et(11, 0),
        )
        signals = {d.signal_name for d in ds if d.action == RoutingAction.ROUTE}
        assert EARNINGS_REPORT_V1 in signals
        assert WHALE_TAIL_V1 in signals

    def test_block_decisions_survive_regime_filter(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("analyst", "target_cut", "negative"), regime="RED")
        assert ds[0].action == RoutingAction.BLOCK  # blocks are not regime-filtered


# ---------------------------------------------------------------------------
# Time-of-day filters
# ---------------------------------------------------------------------------

class TestTimeFilter:
    def test_exit_only_window(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "report", "positive"), time_et=_et(15, 50))
        assert ds[0].action == RoutingAction.SKIP
        assert "exit-only" in ds[0].reason.lower()

    def test_opening_volatility_catalyst_only(self):
        r = RuleBasedRouter(enable_technical_signals=True)
        ds = r.route(
            _Evt("earnings", "report", "positive"),
            time_et=_et(9, 35),
        )
        for d in ds:
            if d.action == RoutingAction.ROUTE:
                assert d.signal_name in CATALYST_SAFE_SIGNALS

    def test_midday_allows_technical(self):
        r = RuleBasedRouter(enable_technical_signals=True)
        ds = r.route(
            _Evt("earnings", "report", "positive"),
            time_et=_et(11, 0),
        )
        signals = {d.signal_name for d in ds if d.action == RoutingAction.ROUTE}
        assert WHALE_TAIL_V1 in signals

    def test_late_session_catalyst_only(self):
        r = RuleBasedRouter(enable_technical_signals=True)
        ds = r.route(
            _Evt("earnings", "report", "positive"),
            time_et=_et(14, 45),
        )
        for d in ds:
            if d.action == RoutingAction.ROUTE:
                assert d.signal_name in CATALYST_SAFE_SIGNALS


# ---------------------------------------------------------------------------
# Audit metadata
# ---------------------------------------------------------------------------

class TestAuditMetadata:
    def test_decision_echoes_event_fields(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "report", "positive", 0.12))
        d = ds[0]
        assert d.category == "earnings"
        assert d.subcategory == "report"
        assert d.sentiment == "positive"
        assert d.priority_modifier == 0.12

    def test_every_decision_has_rule_id(self):
        r = RuleBasedRouter()
        for cat, sub in [
            ("earnings", "report"), ("analyst", "target_cut"),
            ("filing", "8a"), ("m_and_a", "acquires"),
        ]:
            ds = r.route(_Evt(cat, sub, "positive"))
            for d in ds:
                assert d.rule_id, f"missing rule_id for {cat}/{sub}"

    def test_deferred_has_metadata(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("analyst", "target_raise", "positive"))
        deferred = [d for d in ds if d.action == RoutingAction.DEFERRED]
        assert len(deferred) == 1
        assert "deferred_reason" in deferred[0].metadata


# ---------------------------------------------------------------------------
# Priority modifier affects conviction
# ---------------------------------------------------------------------------

class TestConviction:
    def test_higher_pm_higher_conviction(self):
        r = RuleBasedRouter()
        low = r.route(_Evt("earnings", "report", "positive", 0.0))
        high = r.route(_Evt("earnings", "report", "positive", 0.2))
        low_route = [d for d in low if d.action == RoutingAction.ROUTE][0]
        high_route = [d for d in high if d.action == RoutingAction.ROUTE][0]
        assert high_route.conviction >= low_route.conviction

    def test_conviction_bounded_0_1(self):
        r = RuleBasedRouter()
        ds = r.route(_Evt("earnings", "report", "positive", 0.5))
        for d in ds:
            assert 0.0 <= d.conviction <= 1.0
