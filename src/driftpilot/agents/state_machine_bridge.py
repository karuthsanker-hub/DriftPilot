"""Bridge between the DriftPilot state machine and the agent orchestrator.

Adapts state-machine data types (PositionRecord, AllocationCandidate, etc.)
to agent data types (PortfolioSnapshot, PositionSnapshot, CandidateInfo, etc.).

Called from DriftPilotStateMachine.run_once() at three points:
1. Before monitoring: tick_pm (portfolio-level oversight)
2. During monitoring: tick_slot per open position (exit overrides)
3. After scanning: tick_scanner (entry approval)

All calls are no-ops if the orchestrator is None or not running.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from driftpilot.agents.market_data_adapter import MarketDataAdapter
    from driftpilot.agents.orchestrator import AgentOrchestrator
    from driftpilot.execution.slot_allocator import AllocationCandidate
    from driftpilot.settings import DriftPilotSettings
    from driftpilot.storage.repositories import DriftPilotRepository, PositionRecord

from driftpilot.agents.pm_agent import PortfolioSnapshot
from driftpilot.agents.scanner_agent import CandidateInfo, MarketContext
from driftpilot.agents.slot_agent import PositionSnapshot

logger = logging.getLogger(__name__)


def tick_pm_from_repo(
    orchestrator: AgentOrchestrator | None,
    repository: DriftPilotRepository,
    settings: DriftPilotSettings,
) -> int:
    """Build PortfolioSnapshot from repo and run PM tick.

    Returns number of messages processed (0 if orchestrator disabled).
    """
    if orchestrator is None or not orchestrator.running:
        return 0

    try:
        slots = repository.slots.list_all()
        positions = repository.positions.list_open()
        open_count = sum(1 for s in slots if s.status.upper() == "OPEN")
        total_slots = len(slots)

        # Build sector exposure from open positions
        sector_exposure: dict[str, int] = {}
        for pos in positions:
            sector = (pos.metadata or {}).get("sector", "unknown")
            sector_exposure[sector] = sector_exposure.get(sector, 0) + 1

        # Daily PnL from closed positions today
        now = datetime.now(timezone.utc)
        today_iso = now.strftime("%Y-%m-%d")
        try:
            row = repository.connection.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM positions "
                "WHERE status = 'closed' AND closed_at >= ?",
                (today_iso,),
            ).fetchone()
            daily_pnl = float(row["pnl"] if row else 0)
        except Exception:
            daily_pnl = 0.0

        daily_pnl_pct = daily_pnl / max(settings.paper_capital, 1.0)

        # Win/loss streaks from recent trades
        try:
            recent = repository.connection.execute(
                "SELECT realized_pnl FROM positions "
                "WHERE status = 'closed' AND closed_at >= ? "
                "ORDER BY closed_at DESC LIMIT 10",
                (today_iso,),
            ).fetchall()
        except Exception:
            recent = []

        consec_wins = 0
        consec_losses = 0
        last_result = "none"
        for r in recent:
            pnl = float(r["realized_pnl"] or 0)
            if pnl > 0:
                if last_result == "none":
                    last_result = "win"
                if last_result == "win":
                    consec_wins += 1
                else:
                    break
            elif pnl < 0:
                if last_result == "none":
                    last_result = "loss"
                if last_result == "loss":
                    consec_losses += 1
                else:
                    break
            else:
                break

        # Minutes left — estimate from ET market hours
        try:
            from driftpilot.clock import DriftPilotClock

            clock = DriftPilotClock(settings.timezone)
            et_now = clock.to_et(now)
            close_et = et_now.replace(hour=16, minute=0, second=0, microsecond=0)
            minutes_left = max(0, int((close_et - et_now).total_seconds() / 60))
        except Exception:
            minutes_left = 120  # fallback

        snapshot = PortfolioSnapshot(
            open_slots=open_count,
            total_slots=total_slots,
            sector_exposure=sector_exposure,
            daily_pnl_pct=daily_pnl_pct,
            consecutive_wins=consec_wins,
            consecutive_losses=consec_losses,
            minutes_left_in_session=minutes_left,
            last_trade_result=last_result,
            override_count_today=0,  # populated by orchestrator internally
            total_decisions_today=0,
        )

        logger.info(
            "[AGENT-PM] tick: slots=%d/%d pnl=%.2f%% wins=%d losses=%d mins_left=%d sectors=%s",
            open_count, total_slots, daily_pnl_pct * 100, consec_wins,
            consec_losses, minutes_left, sector_exposure,
        )
        n = orchestrator.tick_pm(snapshot)
        logger.info("[AGENT-PM] result: %d messages processed", n)
        return n
    except Exception:
        logger.exception("Agent bridge: tick_pm failed")
        return 0


def tick_slots_from_positions(
    orchestrator: AgentOrchestrator | None,
    positions: list[PositionRecord],
    exit_decisions: dict[int, tuple[str | None, float]],
    settings: DriftPilotSettings,
    market_adapter: MarketDataAdapter | None = None,
) -> dict[int, str]:
    """Run tick_slot for each open position.

    Args:
        orchestrator: The agent orchestrator (or None).
        positions: Open positions from the repository.
        exit_decisions: Map of position.id -> (exit_reason, reference_price)
            from the algo's _exit_signal. None exit_reason means algo says HOLD.
        settings: DriftPilot settings.
        market_adapter: Optional adapter for live market data fields.

    Returns:
        Map of slot_id -> agent action string (for logging).
    """
    if orchestrator is None or not orchestrator.running:
        return {}

    results: dict[int, str] = {}
    for pos in positions:
        if pos.slot_id is None:
            continue

        try:
            algo_exit = exit_decisions.get(pos.id, (None, 0.0))
            algo_says_exit = algo_exit[0] is not None
            reference_price = algo_exit[1] if algo_exit[1] > 0 else pos.entry_price

            metadata = pos.metadata or {}
            sector = str(metadata.get("sector", "unknown"))
            entry = pos.entry_price

            # Use market adapter for live data, fall back to metadata/placeholders
            if market_adapter is not None:
                mkt = market_adapter.compute(
                    symbol=pos.symbol,
                    sector=sector,
                    entry_time=pos.opened_at,
                )
                current_price = mkt.current_price or float(
                    metadata.get("current_price", reference_price)
                )
                last_10_closes = mkt.last_10_closes or [current_price]
                last_10_volumes = mkt.last_10_volumes or [0]
                recent_vol = mkt.recent_vol
                avg_vol = mkt.avg_vol
                rvol = mkt.rvol
                consolidation_bars = mkt.consolidation_bars
                spy_move_pct = mkt.spy_move_pct
                sector_move_pct = mkt.sector_move_pct
                vix = mkt.vix
                new_headlines = mkt.new_headlines
            else:
                current_price = float(
                    metadata.get("current_price", reference_price)
                )
                last_10_closes = [current_price]
                last_10_volumes = [0]
                recent_vol = 0.0
                avg_vol = 0
                rvol = 1.0
                consolidation_bars = 0
                spy_move_pct = 0.0
                sector_move_pct = 0.0
                vix = 0.0
                new_headlines = ""

            unrealized_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0.0
            target_pct = ((pos.target_price - entry) / entry) if entry > 0 else 0.01
            stop_pct = ((entry - pos.stop_price) / entry) if entry > 0 else 0.015

            age_minutes = int(
                (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 60
            )
            high_pct = float(metadata.get("peak_unrealized_pct", unrealized_pct))
            low_pct = float(metadata.get("min_unrealized_pct", min(0, unrealized_pct)))

            snapshot = PositionSnapshot(
                symbol=pos.symbol,
                slot_id=pos.slot_id,
                entry_price=entry,
                current_price=current_price,
                unrealized_pct=unrealized_pct,
                target_pct=target_pct,
                stop_pct=stop_pct,
                hold_minutes=age_minutes,
                max_hold_minutes=settings.max_hold_minutes,
                last_10_closes=last_10_closes,
                last_10_volumes=last_10_volumes,
                high_pct=high_pct,
                low_pct=low_pct,
                consolidation_bars=consolidation_bars,
                recent_vol=recent_vol,
                avg_vol=avg_vol,
                rvol=rvol,
                sector_move_pct=sector_move_pct,
                spy_move_pct=spy_move_pct,
                vix=vix,
                new_headlines=new_headlines,
                signal_name=str(metadata.get("signal_name", settings.active_signal)),
            )

            logger.info(
                "[AGENT-SLOT-%d] tick: %s price=%.2f unrealized=%.2f%% "
                "hold=%dmin algo_exit=%s target=%.2f%% stop=%.2f%% vix=%.1f",
                pos.slot_id, pos.symbol, current_price, unrealized_pct,
                age_minutes, algo_exit[0] or "HOLD", target_pct * 100,
                stop_pct * 100, vix,
            )
            result = orchestrator.tick_slot(pos.slot_id, snapshot, algo_says_exit)
            if result is not None:
                logger.info(
                    "[AGENT-SLOT-%d] verdict: action=%s confidence=%.2f reasoning=%s",
                    pos.slot_id, result.action, result.confidence,
                    result.reasoning[:120] if result.reasoning else "n/a",
                )
                results[pos.slot_id] = result.action
            else:
                logger.info("[AGENT-SLOT-%d] verdict: None (agent disabled or missing)", pos.slot_id)
        except Exception:
            logger.exception(
                "Agent bridge: tick_slot failed for slot %d", pos.slot_id
            )

    return results


def tick_analyst(
    orchestrator: AgentOrchestrator | None,
) -> bool:
    """Run the PM Analyst periodic analysis tick.

    The analyst self-throttles to its configured interval (default 15 min).
    Safe to call every operator cycle — it's a no-op if the interval hasn't elapsed.
    Works even when the agent orchestrator is disabled (analyst only needs Qwen).

    Returns True if an analysis was produced.
    """
    if orchestrator is None:
        return False

    try:
        ran = orchestrator.tick_analyst()
        if ran:
            logger.info("[AGENT-ANALYST] PM analysis completed")
        return ran
    except Exception:
        logger.exception("Agent bridge: tick_analyst failed")
        return False


def tick_scanner_from_candidates(
    orchestrator: AgentOrchestrator | None,
    candidates: list[AllocationCandidate],
    regime: str | None,
    metadata: dict[str, Any],
) -> int:
    """Convert AllocationCandidates to CandidateInfo and run scanner tick.

    Returns number of entries requested (0 if orchestrator disabled).
    """
    if orchestrator is None or not orchestrator.running:
        return 0
    if not candidates:
        return 0

    try:
        agent_candidates = []
        for c in candidates:
            md = c.metadata or {}
            agent_candidates.append(
                CandidateInfo(
                    symbol=c.symbol,
                    signal_name=str(md.get("signal_name", "unknown")),
                    algo_score=c.score,
                    headline=str(md.get("headline", "")),
                    category=str(md.get("category", "unknown")),
                    subcategory=str(md.get("subcategory", "")),
                    sentiment=str(md.get("sentiment", "neutral")),
                    confidence=float(md.get("confidence") or 0.5),
                    priority_modifier=float(md.get("priority_modifier") or 0.0),
                    sector=c.sector,
                    minutes_since_headline=int(md.get("minutes_since_headline") or 0),
                    same_symbol_traded_today=bool(md.get("same_symbol_traded_today", False)),
                    similar_headlines_last_2h=int(md.get("similar_headlines_last_2h") or 0),
                )
            )

        market = MarketContext(
            spy_change_pct=float(metadata.get("spy_return_5m", 0.0)),
            vix=float(metadata.get("vix", 18.0)),
            sector_change_pct=float(metadata.get("sector_return", 0.0)),
        )

        logger.info(
            "[AGENT-SCANNER] tick: %d candidates regime=%s spy=%.2f%% vix=%.1f",
            len(agent_candidates), regime, market.spy_change_pct, market.vix,
        )
        for c in agent_candidates:
            logger.info(
                "[AGENT-SCANNER]   candidate: %s score=%.3f sentiment=%s conf=%.2f headline=%.80s",
                c.symbol, c.algo_score, c.sentiment, c.confidence,
                c.headline,
            )
        n = orchestrator.tick_scanner(agent_candidates, market)
        logger.info("[AGENT-SCANNER] result: %d entries requested", n)
        return n
    except Exception:
        logger.exception("Agent bridge: tick_scanner failed")
        return 0
