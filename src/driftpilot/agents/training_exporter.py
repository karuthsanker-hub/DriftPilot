"""Training Data Exporter — exports agent decisions for fine-tuning.

Reads from the agent_decisions table and produces JSONL files suitable
for supervised fine-tuning of Qwen or Claude. Each row is a single
decision with the full prompt context, LLM response, and outcome.

Supports:
- Filtering by date range, agent name, decision type
- Outcome backfill from closed positions
- Override-only exports for studying where the agent disagreed with algo
- Summary statistics for quality review
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportFilters:
    """Filters for training data export."""

    start_date: str | None = None  # ISO date string
    end_date: str | None = None
    agent_name: str | None = None
    decision_type: str | None = None
    overrides_only: bool = False
    with_outcome_only: bool = False
    symbol: str | None = None
    limit: int = 10_000


@dataclass
class ExportStats:
    """Summary statistics for an export batch."""

    total_decisions: int = 0
    overrides: int = 0
    override_rate: float = 0.0
    outcomes_filled: int = 0
    outcomes_correct: int = 0
    outcomes_incorrect: int = 0
    accuracy: float | None = None
    avg_latency_ms: float = 0.0
    models_used: dict[str, int] = field(default_factory=dict)
    decision_types: dict[str, int] = field(default_factory=dict)
    agents: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TrainingExample:
    """One training example for fine-tuning."""

    decision_id: int
    agent_name: str
    decision_type: str
    symbol: str | None
    slot_id: int | None
    algo_recommendation: str
    agent_decision: str
    is_override: bool
    reasoning: str
    confidence: float | None
    llm_model: str
    llm_latency_ms: int
    prompt_version: str
    inputs: dict[str, Any]
    raw_response: str
    outcome_pnl_pct: float | None
    outcome_correct: bool | None
    created_at: str


class TrainingExporter:
    """Exports agent decisions from SQLite for model fine-tuning."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not self._db_path.exists():
            raise FileNotFoundError(f"Agent DB not found: {self._db_path}")
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def export_decisions(
        self, filters: ExportFilters | None = None
    ) -> Iterator[TrainingExample]:
        """Yield training examples matching the given filters."""
        filters = filters or ExportFilters()
        conn = self._connect()
        try:
            query, params = self._build_query(filters)
            cursor = conn.execute(query, params)
            for row in cursor:
                yield self._row_to_example(row)
        finally:
            conn.close()

    def export_jsonl(
        self,
        output_path: str | Path,
        filters: ExportFilters | None = None,
    ) -> ExportStats:
        """Export training examples as JSONL file. Returns stats."""
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        stats = ExportStats()
        with output.open("w") as f:
            for example in self.export_decisions(filters):
                record = self._example_to_dict(example)
                f.write(json.dumps(record, default=str) + "\n")
                self._update_stats(stats, example)

        self._finalize_stats(stats)
        logger.info(
            "Exported %d training examples to %s (overrides=%d, accuracy=%s)",
            stats.total_decisions,
            output,
            stats.overrides,
            f"{stats.accuracy:.2%}" if stats.accuracy is not None else "N/A",
        )
        return stats

    def get_stats(self, filters: ExportFilters | None = None) -> ExportStats:
        """Get summary statistics without writing a file."""
        stats = ExportStats()
        for example in self.export_decisions(filters):
            self._update_stats(stats, example)
        self._finalize_stats(stats)
        return stats

    def backfill_outcomes(
        self,
        operator_db_path: str | Path,
    ) -> int:
        """Backfill outcome_pnl_pct and outcome_correct from closed positions.

        Joins agent_decisions (entries with symbol) against the operator's
        positions table to find realized PnL for each decision.

        Returns count of decisions updated.
        """
        operator_path = Path(operator_db_path)
        if not operator_path.exists():
            logger.warning("Operator DB not found: %s", operator_path)
            return 0

        conn = self._connect()
        try:
            conn.execute(
                "ATTACH DATABASE ? AS operator", (str(operator_path),)
            )

            # Update entry_approval decisions with position outcomes
            cursor = conn.execute(
                """
                UPDATE agent_decisions
                SET outcome_pnl_pct = (
                    SELECT p.realized_pnl / (p.entry_price * p.quantity)
                    FROM operator.positions p
                    WHERE p.symbol = agent_decisions.symbol
                      AND p.status = 'closed'
                      AND p.opened_at >= agent_decisions.created_at
                    ORDER BY p.opened_at ASC
                    LIMIT 1
                ),
                outcome_correct = CASE
                    WHEN agent_decisions.agent_decision = 'approve'
                         AND (SELECT p.realized_pnl
                              FROM operator.positions p
                              WHERE p.symbol = agent_decisions.symbol
                                AND p.status = 'closed'
                                AND p.opened_at >= agent_decisions.created_at
                              ORDER BY p.opened_at ASC
                              LIMIT 1) > 0
                    THEN 1
                    WHEN agent_decisions.agent_decision = 'deny'
                    THEN NULL  -- Can't know if deny was correct
                    ELSE 0
                END
                WHERE agent_decisions.symbol IS NOT NULL
                  AND agent_decisions.outcome_pnl_pct IS NULL
                  AND agent_decisions.decision_type = 'entry_approval'
                """
            )
            updated = cursor.rowcount
            conn.commit()
            conn.execute("DETACH DATABASE operator")

            logger.info("Backfilled outcomes for %d decisions", updated)
            return updated
        except Exception:
            logger.exception("Failed to backfill outcomes")
            return 0
        finally:
            conn.close()

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent decisions for dashboard display."""
        try:
            conn = self._connect()
        except FileNotFoundError:
            return []
        try:
            rows = conn.execute(
                """
                SELECT id, agent_name, decision_type, symbol, slot_id,
                       algo_recommendation, agent_decision, is_override,
                       reasoning, confidence, llm_model, llm_latency_ms,
                       prompt_version, outcome_pnl_pct, outcome_correct,
                       created_at
                FROM agent_decisions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
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
                    "prompt_version": row["prompt_version"],
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
        except Exception:
            logger.exception("Failed to read recent decisions")
            return []
        finally:
            conn.close()

    def _build_query(
        self, filters: ExportFilters
    ) -> tuple[str, list[Any]]:
        """Build parameterized SQL query from filters."""
        clauses: list[str] = []
        params: list[Any] = []

        if filters.start_date:
            clauses.append("created_at >= ?")
            params.append(filters.start_date)
        if filters.end_date:
            clauses.append("created_at <= ?")
            params.append(filters.end_date)
        if filters.agent_name:
            clauses.append("agent_name = ?")
            params.append(filters.agent_name)
        if filters.decision_type:
            clauses.append("decision_type = ?")
            params.append(filters.decision_type)
        if filters.overrides_only:
            clauses.append("is_override = 1")
        if filters.with_outcome_only:
            clauses.append("outcome_pnl_pct IS NOT NULL")
        if filters.symbol:
            clauses.append("symbol = ?")
            params.append(filters.symbol)

        where = " AND ".join(clauses) if clauses else "1=1"
        query = f"""
            SELECT id, agent_name, decision_type, symbol, slot_id,
                   algo_recommendation, agent_decision, is_override,
                   reasoning, confidence, llm_model, llm_latency_ms,
                   prompt_version, inputs_json, raw_response,
                   outcome_pnl_pct, outcome_correct, created_at
            FROM agent_decisions
            WHERE {where}
            ORDER BY created_at ASC
            LIMIT ?
        """
        params.append(filters.limit)
        return query, params

    def _row_to_example(self, row: sqlite3.Row) -> TrainingExample:
        """Convert a DB row to a TrainingExample."""
        inputs_raw = row["inputs_json"] or "{}"
        try:
            inputs = json.loads(inputs_raw)
        except json.JSONDecodeError:
            inputs = {"_raw": inputs_raw}

        return TrainingExample(
            decision_id=row["id"],
            agent_name=row["agent_name"],
            decision_type=row["decision_type"],
            symbol=row["symbol"],
            slot_id=row["slot_id"],
            algo_recommendation=row["algo_recommendation"],
            agent_decision=row["agent_decision"],
            is_override=bool(row["is_override"]),
            reasoning=row["reasoning"],
            confidence=row["confidence"],
            llm_model=row["llm_model"],
            llm_latency_ms=row["llm_latency_ms"],
            prompt_version=row["prompt_version"],
            inputs=inputs,
            raw_response=row["raw_response"],
            outcome_pnl_pct=row["outcome_pnl_pct"],
            outcome_correct=(
                bool(row["outcome_correct"])
                if row["outcome_correct"] is not None
                else None
            ),
            created_at=row["created_at"],
        )

    def _example_to_dict(self, ex: TrainingExample) -> dict[str, Any]:
        """Convert a TrainingExample to a dict for JSONL serialization."""
        return {
            "decision_id": ex.decision_id,
            "agent_name": ex.agent_name,
            "decision_type": ex.decision_type,
            "symbol": ex.symbol,
            "slot_id": ex.slot_id,
            "algo_recommendation": ex.algo_recommendation,
            "agent_decision": ex.agent_decision,
            "is_override": ex.is_override,
            "reasoning": ex.reasoning,
            "confidence": ex.confidence,
            "llm_model": ex.llm_model,
            "llm_latency_ms": ex.llm_latency_ms,
            "prompt_version": ex.prompt_version,
            "inputs": ex.inputs,
            "raw_response": ex.raw_response,
            "outcome_pnl_pct": ex.outcome_pnl_pct,
            "outcome_correct": ex.outcome_correct,
            "created_at": ex.created_at,
        }

    def _update_stats(self, stats: ExportStats, ex: TrainingExample) -> None:
        """Accumulate stats from one example."""
        stats.total_decisions += 1
        if ex.is_override:
            stats.overrides += 1
        if ex.outcome_pnl_pct is not None:
            stats.outcomes_filled += 1
            if ex.outcome_correct is True:
                stats.outcomes_correct += 1
            elif ex.outcome_correct is False:
                stats.outcomes_incorrect += 1
        stats.avg_latency_ms += ex.llm_latency_ms

        model = ex.llm_model or "unknown"
        stats.models_used[model] = stats.models_used.get(model, 0) + 1
        stats.decision_types[ex.decision_type] = (
            stats.decision_types.get(ex.decision_type, 0) + 1
        )
        stats.agents[ex.agent_name] = stats.agents.get(ex.agent_name, 0) + 1

    def _finalize_stats(self, stats: ExportStats) -> None:
        """Compute derived stats after accumulation."""
        if stats.total_decisions > 0:
            stats.override_rate = stats.overrides / stats.total_decisions
            stats.avg_latency_ms = stats.avg_latency_ms / stats.total_decisions
        else:
            stats.avg_latency_ms = 0.0

        judged = stats.outcomes_correct + stats.outcomes_incorrect
        if judged > 0:
            stats.accuracy = stats.outcomes_correct / judged
