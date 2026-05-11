"""Signal Router — maps catalyst events to the best signal algorithm(s).

Phase 1: RuleBasedRouter — deterministic lookup from the thesis matrix.
No LLM, no broker, no DB, no network. Pure function of (event, regime, time).

The router is shared between live and backtest code. It returns RoutingDecision
objects that downstream code (operator, catalyst_replay) uses to dispatch.

See docs/SIGNAL_ROUTER_AND_PORTFOLIO_MANAGER.md for the full thesis matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from typing import Any


# ---------------------------------------------------------------------------
# Constants — signal names must match the signal registry exactly
# ---------------------------------------------------------------------------

# Catalyst signals (wired for live execution)
EARNINGS_REPORT_V1 = "earnings_report_v1"
FILING_8A_V1 = "filing_8a_v1"

# Technical signals (Phase 3 — not live-routed yet)
APEX_HUNTER_V2_2 = "apex_hunter_v2_2"
WHALE_TAIL_V1 = "whale_tail_v1"
STATIONARY_GHOST_V1 = "stationary_ghost_v1"
RS_DRIFT_V1 = "rs_drift_v1"

# Catalyst-safe signals that can be live-routed today
CATALYST_SAFE_SIGNALS: frozenset[str] = frozenset({
    EARNINGS_REPORT_V1,
    FILING_8A_V1,
})

# Technical signals that require Phase 3 wiring before live routing
TECHNICAL_SIGNALS: frozenset[str] = frozenset({
    APEX_HUNTER_V2_2,
    WHALE_TAIL_V1,
    STATIONARY_GHOST_V1,
    RS_DRIFT_V1,
})

ALL_KNOWN_SIGNALS: frozenset[str] = CATALYST_SAFE_SIGNALS | TECHNICAL_SIGNALS


# ---------------------------------------------------------------------------
# Routing action enum
# ---------------------------------------------------------------------------

class RoutingAction(StrEnum):
    """What the router decided to do with a catalyst event."""
    ROUTE = "ROUTE"        # route to a specific signal
    BLOCK = "BLOCK"        # block the symbol from long entry (anti-signal)
    SKIP = "SKIP"          # no edge — ignore the event
    DEFERRED = "DEFERRED"  # would route to a technical signal, but Phase 3 not wired


# ---------------------------------------------------------------------------
# Routing decision — frozen, auditable
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoutingDecision:
    """One routing decision for a catalyst event."""
    action: RoutingAction
    signal_name: str | None       # which signal to route to (None for BLOCK/SKIP)
    horizon_minutes: int          # trade horizon or block TTL
    conviction: float             # 0.0–1.0 routing conviction
    rule_id: str                  # which rule fired (audit trail)
    reason: str                   # human-readable explanation
    category: str                 # event category echo
    subcategory: str              # event subcategory echo
    sentiment: str | None         # event sentiment echo
    priority_modifier: float      # event priority_modifier echo
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action == RoutingAction.ROUTE and self.signal_name is None:
            raise ValueError("ROUTE action requires a signal_name")
        if self.action == RoutingAction.ROUTE and self.signal_name not in ALL_KNOWN_SIGNALS:
            raise ValueError(f"unknown signal: {self.signal_name}")


# ---------------------------------------------------------------------------
# Regime enum (mirrors signals.regime.Regime but avoids import coupling)
# ---------------------------------------------------------------------------

class RouterRegime(StrEnum):
    GREEN = "GREEN"
    CAUTION = "CAUTION"
    RED = "RED"


# ---------------------------------------------------------------------------
# Time-of-day windows (ET)
# ---------------------------------------------------------------------------

_MARKET_OPEN = time(9, 30)
_OPENING_VOLATILITY_END = time(10, 0)
_TECH_SCAN_OPEN = time(10, 0)
_APEX_SCAN_OPEN = time(10, 30)
_APEX_SCAN_CLOSE = time(14, 30)
_LATE_SESSION_START = time(14, 30)
_EXIT_ONLY_START = time(15, 45)
_MARKET_CLOSE = time(16, 0)


def _is_catalyst_only_window(t: time) -> bool:
    """True during opening volatility or late-session when only catalyst signals are eligible."""
    return t < _OPENING_VOLATILITY_END or t >= _LATE_SESSION_START


def _is_exit_only(t: time) -> bool:
    """True in the final 15 minutes — no new entries."""
    return t >= _EXIT_ONLY_START


def _is_apex_eligible(t: time) -> bool:
    """Apex hunter needs 90-min EWMLR warm-up."""
    return _APEX_SCAN_OPEN <= t < _APEX_SCAN_CLOSE


def _is_tech_eligible(t: time) -> bool:
    """Technical signals' full scan windows (10:00–15:00 ET)."""
    return _TECH_SCAN_OPEN <= t < _LATE_SESSION_START


# ---------------------------------------------------------------------------
# ET timezone helper
# ---------------------------------------------------------------------------

def _require_et(dt: datetime) -> time:
    """Extract ET time from an aware datetime. Raises ValueError on naive."""
    if dt.tzinfo is None:
        raise ValueError(
            "time_et must be timezone-aware. Got naive datetime. "
            "Use datetime.now(ZoneInfo('America/New_York')) or similar."
        )
    # Convert to ET for time-of-day checks
    try:
        from zoneinfo import ZoneInfo
        et = dt.astimezone(ZoneInfo("America/New_York"))
    except ImportError:
        # Fallback: assume caller already passed ET-localized datetime
        et = dt
    return et.time()


# ---------------------------------------------------------------------------
# RuleBasedRouter
# ---------------------------------------------------------------------------

class RuleBasedRouter:
    """Deterministic signal router from the thesis matrix.

    Pure function: no DB, no broker, no LLM, no side effects.
    Same instance can be used in live and backtest code.

    Usage:
        router = RuleBasedRouter()
        decisions = router.route(event, regime="GREEN", time_et=now_et)
    """

    def __init__(self, *, enable_technical_signals: bool = False) -> None:
        """
        Args:
            enable_technical_signals: If True, emit ROUTE for technical signals
                instead of DEFERRED. Only set True after Phase 3 wiring is done.
        """
        self._enable_technical = enable_technical_signals

    def route(
        self,
        event: "CatalystEventLike",
        regime: str | RouterRegime | None = None,
        time_et: datetime | None = None,
    ) -> list[RoutingDecision]:
        """Route a catalyst event to 0-N signal algorithms.

        Args:
            event: Must have .category, .subcategory, .sentiment, .priority_modifier
            regime: Current market regime (GREEN/CAUTION/RED). None = conservative default.
            time_et: Current time as an aware datetime. None = no time filtering.

        Returns:
            List of RoutingDecision. Empty = no action. Can contain ROUTE, BLOCK,
            SKIP, or DEFERRED actions.

        Raises:
            ValueError: If time_et is a naive datetime.
        """
        cat = event.category
        sub = event.subcategory
        sentiment = getattr(event, "sentiment", None)
        pm = getattr(event, "priority_modifier", 0.0) or 0.0

        # Normalize regime
        regime_val: RouterRegime | None = None
        if regime is not None:
            regime_val = RouterRegime(str(regime).upper())

        # Extract ET time if provided
        et_time: time | None = None
        if time_et is not None:
            et_time = _require_et(time_et)

        # Exit-only window — no new entries
        if et_time is not None and _is_exit_only(et_time):
            return [self._skip(
                event, rule_id="time_exit_only",
                reason="Exit-only period (15:45–16:00 ET). No new entries.",
            )]

        # Dispatch by category/subcategory
        key = f"{cat}/{sub}"
        handler = self._DISPATCH.get(key)
        if handler is None:
            return [self._skip(
                event, rule_id="no_rule",
                reason=f"No routing rule for {key}. Event ignored.",
            )]

        decisions = handler(self, event, sentiment, pm, regime_val, et_time)

        # Apply regime filter
        if regime_val is not None:
            decisions = self._apply_regime_filter(decisions, regime_val, et_time)

        # Apply time-of-day filter
        if et_time is not None:
            decisions = self._apply_time_filter(decisions, et_time)

        return decisions if decisions else [self._skip(
            event, rule_id="filtered_out",
            reason="All candidates filtered by regime/time rules.",
        )]

    # -------------------------------------------------------------------
    # Category handlers — each returns list[RoutingDecision]
    # -------------------------------------------------------------------

    def _route_earnings_report(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        if sentiment == "negative":
            return [self._block(
                event, rule_id="earnings_neg_block",
                reason="Negative earnings report — validated anti-signal (0.64× edge). Block long entry.",
                horizon=240,
            )]
        if sentiment == "positive":
            decisions: list[RoutingDecision] = [
                self._route_to(
                    event, signal=EARNINGS_REPORT_V1, horizon=60,
                    conviction=min(1.0, 0.7 + abs(pm)),
                    rule_id="earnings_pos_primary",
                    reason="Positive earnings report → earnings_report_v1 (5.09× validated cell).",
                ),
            ]
            # Secondary: whale_tail if conditions may be right
            decisions.append(self._tech_route(
                event, signal=WHALE_TAIL_V1, horizon=60,
                conviction=0.5,
                rule_id="earnings_pos_whale_secondary",
                reason="Post-earnings compression + high RVOL candidate for whale_tail_v1 (secondary).",
            ))
            return decisions
        # neutral / None
        return [self._route_to(
            event, signal=EARNINGS_REPORT_V1, horizon=60,
            conviction=0.4,
            rule_id="earnings_neutral",
            reason="Neutral earnings report — route to earnings_report_v1 with lower conviction.",
        )]

    def _route_earnings_guidance_up(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        if sentiment == "positive":
            return [self._route_to(
                event, signal=EARNINGS_REPORT_V1, horizon=60,
                conviction=min(1.0, 0.7 + abs(pm)),
                rule_id="guidance_up_pos",
                reason="Raised guidance — treat like positive earnings/report.",
            )]
        return [self._route_to(
            event, signal=EARNINGS_REPORT_V1, horizon=60,
            conviction=0.4,
            rule_id="guidance_up_neutral",
            reason="Guidance up but non-positive sentiment — lower conviction.",
        )]

    def _route_earnings_guidance_down(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        return [self._block(
            event, rule_id="guidance_down_block",
            reason="Guidance down — block long entries for 4 hours.",
            horizon=240,
        )]

    def _route_earnings_beat(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        # Earnings beat is strongly positive — treat like positive earnings/report
        return [self._route_to(
            event, signal=EARNINGS_REPORT_V1, horizon=60,
            conviction=min(1.0, 0.8 + abs(pm)),
            rule_id="earnings_beat",
            reason="Earnings beat → earnings_report_v1 (strong positive catalyst).",
        )]

    def _route_earnings_miss(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        return [self._block(
            event, rule_id="earnings_miss_block",
            reason="Earnings miss — block long entries for 4 hours.",
            horizon=240,
        )]

    def _route_analyst_target_raise(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        if sentiment == "neutral":
            return [self._skip(
                event, rule_id="target_raise_neutral_skip",
                reason="Neutral on target_raise — 82% are positive, neutral means Qwen unsure. No edge.",
            )]
        if sentiment == "positive":
            return [self._tech_route(
                event, signal=APEX_HUNTER_V2_2, horizon=60,
                conviction=min(1.0, 0.7 + abs(pm)),
                rule_id="target_raise_pos_apex",
                reason="Target raise + positive sentiment → apex_hunter_v2_2 (institutional accumulation).",
            )]
        # negative — unlikely but defensive
        return [self._skip(
            event, rule_id="target_raise_neg_skip",
            reason="Negative sentiment on target_raise — contradictory signal, skip.",
        )]

    def _route_analyst_target_cut(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        return [self._block(
            event, rule_id="target_cut_block",
            reason="Analyst target cut — validated 2.91× absolute move @ 240m. Block all longs 4h.",
            horizon=240,
        )]

    def _route_analyst_upgrade(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        if sentiment == "positive":
            return [self._tech_route(
                event, signal=APEX_HUNTER_V2_2, horizon=60,
                conviction=min(1.0, 0.7 + abs(pm)),
                rule_id="upgrade_pos_apex",
                reason="Analyst upgrade + positive → apex_hunter_v2_2.",
            )]
        return [self._skip(
            event, rule_id="upgrade_non_pos_skip",
            reason="Analyst upgrade without positive sentiment — skip.",
        )]

    def _route_analyst_downgrade(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        return [self._block(
            event, rule_id="downgrade_block",
            reason="Analyst downgrade — confirmed anti-signal (0.41× @ 1day). Block 1 day.",
            horizon=1440,
        )]

    def _route_filing_8a(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        if sentiment == "negative":
            return [self._block(
                event, rule_id="filing_8a_neg_block",
                reason="Negative filing/8a — block long entry.",
                horizon=60,
            )]
        decisions: list[RoutingDecision] = [
            self._route_to(
                event, signal=FILING_8A_V1, horizon=60,
                conviction=0.6 if sentiment == "positive" else 0.4,
                rule_id="filing_8a_primary",
                reason=f"Filing/8a ({sentiment or 'unknown'}) → filing_8a_v1 (2.05× validated cell, N=256).",
            ),
        ]
        # Secondary: stationary_ghost if positive (mean-reversion on pullback)
        if sentiment == "positive":
            decisions.append(self._tech_route(
                event, signal=STATIONARY_GHOST_V1, horizon=20,
                conviction=0.4,
                rule_id="filing_8a_pos_ghost_secondary",
                reason="Positive filing + potential pullback → stationary_ghost_v1 (secondary).",
            ))
        return decisions

    def _route_m_and_a_acquires(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        if sentiment == "positive":
            return [self._tech_route(
                event, signal=RS_DRIFT_V1, horizon=1440,
                conviction=min(1.0, 0.7 + abs(pm)),
                rule_id="m_and_a_pos_rs_drift",
                reason="M&A acquisition + positive → rs_drift_v1 (multi-day RS catalyst).",
            )]
        return [self._skip(
            event, rule_id="m_and_a_non_pos_skip",
            reason="M&A without positive sentiment — uncertain, skip.",
        )]

    def _route_m_and_a_merger(
        self, event: "CatalystEventLike", sentiment: str | None,
        pm: float, regime: RouterRegime | None, et_time: time | None,
    ) -> list[RoutingDecision]:
        # Same as acquires
        return self._route_m_and_a_acquires(event, sentiment, pm, regime, et_time)

    # -------------------------------------------------------------------
    # Dispatch table
    # -------------------------------------------------------------------

    _DISPATCH: dict[str, Any] = {
        "earnings/report": _route_earnings_report,
        "earnings/guidance_up": _route_earnings_guidance_up,
        "earnings/guidance_down": _route_earnings_guidance_down,
        "earnings/beat": _route_earnings_beat,
        "earnings/miss": _route_earnings_miss,
        "analyst/target_raise": _route_analyst_target_raise,
        "analyst/target_cut": _route_analyst_target_cut,
        "analyst/upgrade": _route_analyst_upgrade,
        "analyst/downgrade": _route_analyst_downgrade,
        "filing/8a": _route_filing_8a,
        "m_and_a/acquires": _route_m_and_a_acquires,
        "m_and_a/merger": _route_m_and_a_merger,
    }

    # -------------------------------------------------------------------
    # Regime filter
    # -------------------------------------------------------------------

    def _apply_regime_filter(
        self, decisions: list[RoutingDecision],
        regime: RouterRegime, et_time: time | None,
    ) -> list[RoutingDecision]:
        """Apply regime-based filtering to routing decisions."""
        if regime == RouterRegime.RED:
            # RED: only catalyst-pure signals. Block all technical.
            filtered = []
            for d in decisions:
                if d.action != RoutingAction.ROUTE:
                    filtered.append(d)
                elif d.signal_name in CATALYST_SAFE_SIGNALS:
                    filtered.append(d)
                else:
                    # Downgrade technical routes to SKIP in RED regime
                    filtered.append(RoutingDecision(
                        action=RoutingAction.SKIP,
                        signal_name=None,
                        horizon_minutes=d.horizon_minutes,
                        conviction=0.0,
                        rule_id=f"{d.rule_id}__regime_red_block",
                        reason=f"RED regime — {d.signal_name} blocked. Only catalyst signals allowed.",
                        category=d.category,
                        subcategory=d.subcategory,
                        sentiment=d.sentiment,
                        priority_modifier=d.priority_modifier,
                        metadata={**d.metadata, "regime_filtered": True},
                    ))
            return filtered
        # GREEN/CAUTION: all signals eligible (CAUTION preference is for
        # the Portfolio Manager to handle via sizing, not the router).
        return decisions

    # -------------------------------------------------------------------
    # Time-of-day filter
    # -------------------------------------------------------------------

    def _apply_time_filter(
        self, decisions: list[RoutingDecision], et_time: time,
    ) -> list[RoutingDecision]:
        """Apply time-of-day filtering to routing decisions."""
        if not _is_catalyst_only_window(et_time):
            return decisions  # full window — all eligible

        # Opening volatility (pre-10:00) or late session (14:30+):
        # only catalyst signals allowed.
        filtered = []
        for d in decisions:
            if d.action != RoutingAction.ROUTE:
                filtered.append(d)
            elif d.signal_name in CATALYST_SAFE_SIGNALS:
                filtered.append(d)
            else:
                filtered.append(RoutingDecision(
                    action=RoutingAction.SKIP,
                    signal_name=None,
                    horizon_minutes=d.horizon_minutes,
                    conviction=0.0,
                    rule_id=f"{d.rule_id}__time_filtered",
                    reason=f"Catalyst-only window ({et_time.strftime('%H:%M')} ET) — {d.signal_name} not eligible.",
                    category=d.category,
                    subcategory=d.subcategory,
                    sentiment=d.sentiment,
                    priority_modifier=d.priority_modifier,
                    metadata={**d.metadata, "time_filtered": True},
                ))
        return filtered

    # -------------------------------------------------------------------
    # Decision builders
    # -------------------------------------------------------------------

    def _route_to(
        self, event: "CatalystEventLike", *, signal: str, horizon: int,
        conviction: float, rule_id: str, reason: str,
    ) -> RoutingDecision:
        return RoutingDecision(
            action=RoutingAction.ROUTE,
            signal_name=signal,
            horizon_minutes=horizon,
            conviction=conviction,
            rule_id=rule_id,
            reason=reason,
            category=event.category,
            subcategory=event.subcategory,
            sentiment=getattr(event, "sentiment", None),
            priority_modifier=getattr(event, "priority_modifier", 0.0) or 0.0,
        )

    def _tech_route(
        self, event: "CatalystEventLike", *, signal: str, horizon: int,
        conviction: float, rule_id: str, reason: str,
    ) -> RoutingDecision:
        """Route to a technical signal — DEFERRED unless Phase 3 enabled."""
        if self._enable_technical:
            return self._route_to(
                event, signal=signal, horizon=horizon,
                conviction=conviction, rule_id=rule_id, reason=reason,
            )
        return RoutingDecision(
            action=RoutingAction.DEFERRED,
            signal_name=signal,
            horizon_minutes=horizon,
            conviction=conviction,
            rule_id=rule_id,
            reason=f"DEFERRED (Phase 3): {reason}",
            category=event.category,
            subcategory=event.subcategory,
            sentiment=getattr(event, "sentiment", None),
            priority_modifier=getattr(event, "priority_modifier", 0.0) or 0.0,
            metadata={"deferred_reason": "technical_signal_not_wired"},
        )

    def _block(
        self, event: "CatalystEventLike", *, rule_id: str, reason: str,
        horizon: int,
    ) -> RoutingDecision:
        return RoutingDecision(
            action=RoutingAction.BLOCK,
            signal_name=None,
            horizon_minutes=horizon,
            conviction=0.0,
            rule_id=rule_id,
            reason=reason,
            category=event.category,
            subcategory=event.subcategory,
            sentiment=getattr(event, "sentiment", None),
            priority_modifier=getattr(event, "priority_modifier", 0.0) or 0.0,
        )

    def _skip(
        self, event: "CatalystEventLike", *, rule_id: str, reason: str,
    ) -> RoutingDecision:
        return RoutingDecision(
            action=RoutingAction.SKIP,
            signal_name=None,
            horizon_minutes=0,
            conviction=0.0,
            rule_id=rule_id,
            reason=reason,
            category=event.category,
            subcategory=event.subcategory,
            sentiment=getattr(event, "sentiment", None),
            priority_modifier=getattr(event, "priority_modifier", 0.0) or 0.0,
        )


# ---------------------------------------------------------------------------
# Protocol — what .route() expects from an event (duck typing)
# ---------------------------------------------------------------------------

class CatalystEventLike:
    """Structural type for events the router accepts.

    Any object with these attributes works — CatalystEvent, a test stub,
    or a backtest replay row. No import coupling to catalyst.event.
    """
    category: str
    subcategory: str
    sentiment: str | None
    priority_modifier: float
