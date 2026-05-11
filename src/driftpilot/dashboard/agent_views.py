"""Agent dashboard view models — data payloads for the /agents page.

Reads from the agent message bus SQLite to provide:
- Agent state overview (PM, Scanner, Slot agents)
- Recent decisions with override flags
- Override rate gauge
- Message bus activity
- Training data export stats
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def agent_dashboard_payload(
    agent_db_path: str | Path = "data/driftpilot/agent_messages.sqlite3",
) -> dict[str, Any]:
    """Build the full payload for the /agents dashboard page."""
    p = Path(agent_db_path)
    if not p.exists():
        return _empty_payload("Agent database not found")

    try:
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        return _empty_payload(f"Cannot open agent DB: {exc}")

    try:
        return {
            "enabled": True,
            "error": None,
            "agents": _agent_states(conn),
            "override_rate": _override_rate(conn),
            "recent_decisions": _recent_decisions(conn, limit=30),
            "message_activity": _message_activity(conn),
            "daily_summary": _daily_summary(conn),
        }
    except Exception as exc:
        logger.exception("Failed to build agent dashboard payload")
        return _empty_payload(f"Error reading agent data: {exc}")
    finally:
        conn.close()


def agent_decision_detail(
    decision_id: int,
    agent_db_path: str | Path = "data/driftpilot/agent_messages.sqlite3",
) -> dict[str, Any]:
    """Get full detail for a single decision (for modal/drill-down)."""
    p = Path(agent_db_path)
    if not p.exists():
        return {"found": False, "error": "Agent database not found"}

    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, agent_name, decision_type, symbol, slot_id,
                   algo_recommendation, agent_decision, is_override,
                   reasoning, confidence, llm_model, llm_latency_ms,
                   prompt_version, inputs_json, raw_response,
                   outcome_pnl_pct, outcome_correct, created_at
            FROM agent_decisions
            WHERE id = ?
            """,
            (decision_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"found": False, "error": "Decision not found"}

    return {
        "found": True,
        "decision": {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "decision_type": row["decision_type"],
            "symbol": row["symbol"],
            "slot_id": row["slot_id"],
            "algo_recommendation": row["algo_recommendation"],
            "agent_decision": row["agent_decision"],
            "is_override": bool(row["is_override"]),
            "reasoning": row["reasoning"],
            "confidence": row["confidence"],
            "llm_model": row["llm_model"],
            "llm_latency_ms": row["llm_latency_ms"],
            "prompt_version": row["prompt_version"],
            "inputs": _decode_json(row["inputs_json"]),
            "raw_response": row["raw_response"],
            "outcome_pnl_pct": row["outcome_pnl_pct"],
            "outcome_correct": (
                bool(row["outcome_correct"])
                if row["outcome_correct"] is not None
                else None
            ),
            "created_at": row["created_at"],
        },
    }


def _agent_states(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Current state of all agents from agent_state table."""
    try:
        rows = conn.execute(
            """
            SELECT agent_name, status, last_tick_at,
                   consecutive_wins, consecutive_losses,
                   override_count_today, total_decisions_today,
                   metadata_json, updated_at
            FROM agent_state
            ORDER BY agent_name
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    agents = []
    now = datetime.now(timezone.utc)
    for row in rows:
        updated = row["updated_at"]
        try:
            updated_dt = datetime.fromisoformat(updated)
            age_seconds = (now - updated_dt).total_seconds()
        except (TypeError, ValueError):
            age_seconds = None

        metadata = _decode_json(row["metadata_json"]) or {}
        agents.append({
            "name": row["agent_name"],
            "status": row["status"],
            "last_tick_at": row["last_tick_at"],
            "consecutive_wins": row["consecutive_wins"],
            "consecutive_losses": row["consecutive_losses"],
            "override_count_today": row["override_count_today"],
            "total_decisions_today": row["total_decisions_today"],
            "heartbeat_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "healthy": age_seconds is not None and age_seconds < 120,
            "metadata": metadata,
        })
    return agents


def _override_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    """Today's override rate across all agents."""
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN is_override = 1 THEN 1 ELSE 0 END) AS overrides
            FROM agent_decisions
            WHERE created_at >= ?
            """,
            (today_iso,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {"rate": 0.0, "total": 0, "overrides": 0, "limit": 0.20}

    total = int(row["total"] or 0)
    overrides = int(row["overrides"] or 0)
    rate = overrides / total if total > 0 else 0.0
    return {
        "rate": round(rate, 4),
        "total": total,
        "overrides": overrides,
        "limit": 0.20,
        "within_limit": rate <= 0.20,
    }


def _recent_decisions(
    conn: sqlite3.Connection, limit: int = 30
) -> list[dict[str, Any]]:
    """Most recent agent decisions for the activity feed."""
    try:
        rows = conn.execute(
            """
            SELECT id, agent_name, decision_type, symbol, slot_id,
                   algo_recommendation, agent_decision, is_override,
                   reasoning, confidence, llm_model, llm_latency_ms,
                   outcome_pnl_pct, outcome_correct, created_at
            FROM agent_decisions
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    return [
        {
            "id": row["id"],
            "agent_name": row["agent_name"],
            "decision_type": row["decision_type"],
            "symbol": row["symbol"],
            "slot_id": row["slot_id"],
            "algo_recommendation": row["algo_recommendation"],
            "agent_decision": row["agent_decision"],
            "is_override": bool(row["is_override"]),
            "reasoning": (row["reasoning"] or "")[:200],
            "confidence": row["confidence"],
            "llm_model": row["llm_model"],
            "llm_latency_ms": row["llm_latency_ms"],
            "outcome_pnl_pct": row["outcome_pnl_pct"],
            "outcome_correct": (
                bool(row["outcome_correct"])
                if row["outcome_correct"] is not None
                else None
            ),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _message_activity(conn: sqlite3.Connection) -> dict[str, Any]:
    """Message bus activity summary."""
    try:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_messages WHERE status = 'pending'"
        ).fetchone()
        total_today = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_messages WHERE created_at >= ?",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d"),),
        ).fetchone()
        by_type = conn.execute(
            """
            SELECT msg_type, COUNT(*) AS n
            FROM agent_messages
            WHERE created_at >= ?
            GROUP BY msg_type
            ORDER BY n DESC
            """,
            (datetime.now(timezone.utc).strftime("%Y-%m-%d"),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"pending": 0, "today_total": 0, "by_type": {}}

    return {
        "pending": int(pending["n"] or 0),
        "today_total": int(total_today["n"] or 0),
        "by_type": {row["msg_type"]: int(row["n"]) for row in by_type},
    }


def _daily_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate stats for today's agent activity."""
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN is_override = 1 THEN 1 ELSE 0 END) AS overrides,
                   SUM(CASE WHEN agent_decision = 'approve' THEN 1 ELSE 0 END) AS approvals,
                   SUM(CASE WHEN agent_decision = 'deny' THEN 1 ELSE 0 END) AS denials,
                   AVG(llm_latency_ms) AS avg_latency,
                   AVG(confidence) AS avg_confidence,
                   SUM(CASE WHEN outcome_correct = 1 THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN outcome_correct = 0 THEN 1 ELSE 0 END) AS incorrect
            FROM agent_decisions
            WHERE created_at >= ?
            """,
            (today_iso,),
        ).fetchone()
    except sqlite3.OperationalError:
        return _empty_daily_summary()

    total = int(row["total"] or 0)
    if total == 0:
        return _empty_daily_summary()

    correct = int(row["correct"] or 0)
    incorrect = int(row["incorrect"] or 0)
    judged = correct + incorrect

    return {
        "total_decisions": total,
        "overrides": int(row["overrides"] or 0),
        "approvals": int(row["approvals"] or 0),
        "denials": int(row["denials"] or 0),
        "avg_latency_ms": round(float(row["avg_latency"] or 0), 1),
        "avg_confidence": round(float(row["avg_confidence"] or 0), 3),
        "accuracy": round(correct / judged, 3) if judged > 0 else None,
        "outcomes_judged": judged,
    }


def _empty_daily_summary() -> dict[str, Any]:
    return {
        "total_decisions": 0,
        "overrides": 0,
        "approvals": 0,
        "denials": 0,
        "avg_latency_ms": 0.0,
        "avg_confidence": 0.0,
        "accuracy": None,
        "outcomes_judged": 0,
    }


def _empty_payload(error: str) -> dict[str, Any]:
    return {
        "enabled": False,
        "error": error,
        "agents": [],
        "override_rate": {"rate": 0.0, "total": 0, "overrides": 0, "limit": 0.20, "within_limit": True},
        "recent_decisions": [],
        "message_activity": {"pending": 0, "today_total": 0, "by_type": {}},
        "daily_summary": _empty_daily_summary(),
    }


def _decode_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return data if isinstance(data, dict) else {"value": data}
