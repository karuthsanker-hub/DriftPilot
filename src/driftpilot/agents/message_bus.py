"""SQLite-backed A2A message bus for agent communication.

All inter-agent messages are persisted to SQLite for durability,
observability, and training-data export. Messages expire after a
configurable TTL (default 5 minutes).

Thread-safe for async usage via asyncio.Lock on writes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from .models import AgentMessage, MessageStatus, MessageType

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT NOT NULL UNIQUE,
    msg_type TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    correlation_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    expired_at TEXT,
    CONSTRAINT chk_msg_type CHECK (msg_type IN (
        'ENTRY_REQUEST', 'ENTRY_DECISION', 'ASSIGNMENT',
        'TARGET_RAISE_REQUEST', 'TARGET_RAISE_DECISION',
        'PARTIAL_PROFIT_REQUEST', 'PARTIAL_PROFIT_DECISION',
        'EARLY_CUT_REQUEST', 'EARLY_CUT_DECISION',
        'FORCE_EXIT', 'EXIT_REPORT', 'SESSION_ADAPTATION'
    )),
    CONSTRAINT chk_status CHECK (status IN ('pending', 'acked', 'processed', 'expired'))
);
CREATE INDEX IF NOT EXISTS idx_msg_to_status ON agent_messages(to_agent, status, created_at);
CREATE INDEX IF NOT EXISTS idx_msg_correlation ON agent_messages(correlation_id);

CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    symbol TEXT,
    slot_id INTEGER,
    algo_recommendation TEXT NOT NULL,
    agent_decision TEXT NOT NULL,
    is_override INTEGER NOT NULL DEFAULT 0,
    reasoning TEXT NOT NULL,
    confidence REAL,
    llm_model TEXT NOT NULL,
    llm_latency_ms INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    outcome_pnl_pct REAL,
    outcome_correct INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_agent ON agent_decisions(agent_name, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_override ON agent_decisions(is_override, outcome_correct);

CREATE TABLE IF NOT EXISTS agent_session_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_date TEXT NOT NULL,
    param_name TEXT NOT NULL,
    old_value REAL NOT NULL,
    new_value REAL NOT NULL,
    reason TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_state (
    agent_name TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'idle',
    last_tick_at TEXT,
    consecutive_wins INTEGER NOT NULL DEFAULT 0,
    consecutive_losses INTEGER NOT NULL DEFAULT 0,
    override_count_today INTEGER NOT NULL DEFAULT 0,
    total_decisions_today INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_str(dt: datetime) -> str:
    return dt.isoformat()


def _str_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class MessageBus:
    """SQLite-backed message bus for inter-agent communication."""

    def __init__(
        self,
        db_path: str | Path = "data/driftpilot/agent_messages.sqlite3",
        ttl_seconds: int = 300,
    ) -> None:
        self._db_path = Path(db_path)
        self._ttl_seconds = ttl_seconds
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        return conn

    def initialize(self) -> None:
        """Ensure the DB and schema exist. Idempotent."""
        _ = self.conn

    def send(self, message: AgentMessage) -> str:
        """Persist a message to the bus. Returns msg_id."""
        self.conn.execute(
            """
            INSERT INTO agent_messages
                (msg_id, msg_type, from_agent, to_agent, correlation_id,
                 status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.msg_id,
                message.msg_type.value,
                message.from_agent,
                message.to_agent,
                message.correlation_id,
                message.status.value,
                json.dumps(message.payload, default=str),
                _dt_to_str(message.created_at),
            ),
        )
        self.conn.commit()
        logger.debug(
            "msg_bus.send: %s %s→%s [%s]",
            message.msg_type.value,
            message.from_agent,
            message.to_agent,
            message.msg_id[:8],
        )
        return message.msg_id

    def poll(
        self,
        to_agent: str,
        msg_types: Sequence[MessageType] | None = None,
        *,
        limit: int = 50,
    ) -> list[AgentMessage]:
        """Fetch pending messages for an agent. Expires stale ones first."""
        self._expire_stale()

        placeholders = ""
        params: list[str | int] = [to_agent]
        if msg_types:
            placeholders = " AND msg_type IN ({})".format(
                ",".join("?" for _ in msg_types)
            )
            params.extend(mt.value for mt in msg_types)

        rows = self.conn.execute(
            f"""
            SELECT msg_id, msg_type, from_agent, to_agent, correlation_id,
                   status, payload_json, created_at, processed_at, expired_at
            FROM agent_messages
            WHERE to_agent = ? AND status = 'pending'{placeholders}
            ORDER BY created_at ASC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()

        return [self._row_to_message(row) for row in rows]

    def ack(self, msg_id: str) -> None:
        """Mark a message as acknowledged (being processed)."""
        self.conn.execute(
            "UPDATE agent_messages SET status = 'acked' WHERE msg_id = ?",
            (msg_id,),
        )
        self.conn.commit()

    def mark_processed(self, msg_id: str) -> None:
        """Mark a message as fully processed."""
        now = _dt_to_str(_now_utc())
        self.conn.execute(
            "UPDATE agent_messages SET status = 'processed', processed_at = ? WHERE msg_id = ?",
            (now, msg_id),
        )
        self.conn.commit()

    def get_response(self, correlation_id: str) -> AgentMessage | None:
        """Get a processed response for a given correlation_id."""
        row = self.conn.execute(
            """
            SELECT msg_id, msg_type, from_agent, to_agent, correlation_id,
                   status, payload_json, created_at, processed_at, expired_at
            FROM agent_messages
            WHERE correlation_id = ? AND status IN ('pending', 'acked', 'processed')
            AND msg_type LIKE '%_DECISION' OR msg_type IN ('FORCE_EXIT', 'ASSIGNMENT')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (correlation_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_message(row)

    def count_pending(self, to_agent: str) -> int:
        """Count pending messages for an agent."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE to_agent = ? AND status = 'pending'",
            (to_agent,),
        ).fetchone()
        return row[0] if row else 0

    def log_decision(
        self,
        agent_name: str,
        decision_type: str,
        algo_recommendation: str,
        agent_decision: str,
        reasoning: str,
        llm_model: str,
        llm_latency_ms: int,
        prompt_version: str,
        inputs_json: dict,
        raw_response: str,
        *,
        symbol: str | None = None,
        slot_id: int | None = None,
        is_override: bool = False,
        confidence: float | None = None,
    ) -> int:
        """Log an agent decision for training data export."""
        now = _dt_to_str(_now_utc())
        cursor = self.conn.execute(
            """
            INSERT INTO agent_decisions
                (agent_name, decision_type, symbol, slot_id,
                 algo_recommendation, agent_decision, is_override,
                 reasoning, confidence, llm_model, llm_latency_ms,
                 prompt_version, inputs_json, raw_response, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_name,
                decision_type,
                symbol,
                slot_id,
                algo_recommendation,
                agent_decision,
                1 if is_override else 0,
                reasoning,
                confidence,
                llm_model,
                llm_latency_ms,
                prompt_version,
                json.dumps(inputs_json, default=str),
                raw_response,
                now,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def update_agent_state(
        self,
        agent_name: str,
        *,
        status: str = "running",
        consecutive_wins: int | None = None,
        consecutive_losses: int | None = None,
        override_count_today: int | None = None,
        total_decisions_today: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Upsert agent runtime state."""
        now = _dt_to_str(_now_utc())
        existing = self.conn.execute(
            "SELECT agent_name FROM agent_state WHERE agent_name = ?",
            (agent_name,),
        ).fetchone()

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO agent_state
                    (agent_name, status, last_tick_at, consecutive_wins,
                     consecutive_losses, override_count_today,
                     total_decisions_today, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_name,
                    status,
                    now,
                    consecutive_wins or 0,
                    consecutive_losses or 0,
                    override_count_today or 0,
                    total_decisions_today or 0,
                    json.dumps(metadata or {}),
                    now,
                ),
            )
        else:
            updates = ["status = ?", "last_tick_at = ?", "updated_at = ?"]
            params: list[str | int] = [status, now, now]
            if consecutive_wins is not None:
                updates.append("consecutive_wins = ?")
                params.append(consecutive_wins)
            if consecutive_losses is not None:
                updates.append("consecutive_losses = ?")
                params.append(consecutive_losses)
            if override_count_today is not None:
                updates.append("override_count_today = ?")
                params.append(override_count_today)
            if total_decisions_today is not None:
                updates.append("total_decisions_today = ?")
                params.append(total_decisions_today)
            if metadata is not None:
                updates.append("metadata_json = ?")
                params.append(json.dumps(metadata))
            params.append(agent_name)
            self.conn.execute(
                f"UPDATE agent_state SET {', '.join(updates)} WHERE agent_name = ?",
                params,
            )
        self.conn.commit()

    def get_agent_state(self, agent_name: str) -> dict | None:
        """Get current state for an agent."""
        row = self.conn.execute(
            "SELECT * FROM agent_state WHERE agent_name = ?",
            (agent_name,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_override_rate(self, agent_name: str | None = None) -> float:
        """Calculate today's override rate for a specific agent or all agents."""
        where = "WHERE created_at >= date('now', 'start of day')"
        params: list[str] = []
        if agent_name:
            where += " AND agent_name = ?"
            params.append(agent_name)

        row = self.conn.execute(
            f"""
            SELECT
                COALESCE(SUM(is_override), 0) AS overrides,
                COUNT(*) AS total
            FROM agent_decisions
            {where}
            """,
            params,
        ).fetchone()

        if row is None or row["total"] == 0:
            return 0.0
        return row["overrides"] / row["total"]

    def _expire_stale(self) -> None:
        """Expire messages older than TTL."""
        cutoff = _now_utc() - timedelta(seconds=self._ttl_seconds)
        now = _dt_to_str(_now_utc())
        self.conn.execute(
            """
            UPDATE agent_messages
            SET status = 'expired', expired_at = ?
            WHERE status = 'pending' AND created_at < ?
            """,
            (now, _dt_to_str(cutoff)),
        )
        self.conn.commit()

    def _row_to_message(self, row: sqlite3.Row) -> AgentMessage:
        return AgentMessage(
            msg_id=row["msg_id"],
            msg_type=MessageType(row["msg_type"]),
            from_agent=row["from_agent"],
            to_agent=row["to_agent"],
            correlation_id=row["correlation_id"],
            status=MessageStatus(row["status"]),
            payload=json.loads(row["payload_json"]),
            created_at=_str_to_dt(row["created_at"]),
            processed_at=_str_to_dt(row["processed_at"]) if row["processed_at"] else None,
            expired_at=_str_to_dt(row["expired_at"]) if row["expired_at"] else None,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
