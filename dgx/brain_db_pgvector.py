"""PgVector Brain DB — PostgreSQL + pgvector backend for the Trading Brain.

Drop-in replacement for the ChromaDB-based BrainDB. Uses a single PostgreSQL
database with the pgvector extension for both experience vectors and skill
vectors, plus structured data (skills, reflections) in regular tables.

Advantages over ChromaDB:
- SQL joins between experiences and skills
- ACID transactions
- Better concurrent access from multiple workers
- Production-grade backup/replication via pg_dump / streaming replication
- Native date-range queries (no need for ChromaDB's $gte hack)

Requirements:
    pip install asyncpg pgvector psycopg[binary]  # psycopg3 sync driver
    # PostgreSQL must have: CREATE EXTENSION vector;

Usage:
    db = PgVectorBrainDB("postgresql://user:pass@localhost:5432/brain")
    # Same interface as BrainDB — see brain_db.py
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from brain_db import Experience, Skill, SimilarExperience

logger = logging.getLogger("brain.db.pgvector")

# Default connection string (DGX Spark local postgres)
DEFAULT_DSN = "postgresql://brain:brain@localhost:5432/brain"

# Embedding dimension (all-MiniLM-L6-v2 = 384)
EMBEDDING_DIM = 384


class PgVectorBrainDB:
    """PostgreSQL + pgvector storage for the Trading Brain.

    Same public interface as BrainDB (ChromaDB-based) so the brain_server
    can switch backends transparently.
    """

    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=True)
        register_vector(self._conn)
        self._init_schema()

        stats = self.get_stats()
        logger.info(
            "pgvector_brain_db_ready",
            extra={
                "dsn": dsn.split("@")[-1],  # hide credentials
                "experience_count": stats["total_experiences"],
                "skill_count": stats["total_skills"],
            },
        )

    def _get_conn(self) -> psycopg.Connection:
        """Return a live connection, reconnecting if needed."""
        if self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
            register_vector(self._conn)
        return self._conn

    def _init_schema(self) -> None:
        """Create tables and pgvector extension if they don't exist."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS experiences (
                    experience_id TEXT PRIMARY KEY,
                    timestamp     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    exp_type      TEXT NOT NULL,
                    context       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    decision      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    outcome       JSONB,
                    metadata      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    symbol        TEXT GENERATED ALWAYS AS (context->>'symbol') STORED,
                    signal        TEXT GENERATED ALWAYS AS (context->>'signal') STORED,
                    has_outcome   BOOLEAN GENERATED ALWAYS AS (outcome IS NOT NULL) STORED,
                    embedding     vector({EMBEDDING_DIM})
                )
            """)

            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS skills (
                    skill_id       TEXT PRIMARY KEY,
                    title          TEXT NOT NULL,
                    rule           TEXT NOT NULL,
                    evidence_json  TEXT NOT NULL DEFAULT '[]',
                    category       TEXT NOT NULL,
                    confidence     REAL NOT NULL DEFAULT 0.5,
                    status         TEXT NOT NULL DEFAULT 'pending_verification',
                    effectiveness  REAL,
                    applies_to     TEXT NOT NULL DEFAULT 'pm_entry_approval',
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    retired_at     TIMESTAMPTZ,
                    retire_reason  TEXT,
                    embedding      vector({EMBEDDING_DIM})
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS reflections (
                    id                   SERIAL PRIMARY KEY,
                    date                 TEXT NOT NULL,
                    summary_json         TEXT NOT NULL,
                    skills_created       INTEGER DEFAULT 0,
                    skills_retired       INTEGER DEFAULT 0,
                    experiences_analyzed INTEGER DEFAULT 0,
                    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # Indexes for vector similarity search (IVFFlat for speed)
            # Using cosine distance (<=> operator)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_experiences_embedding
                ON experiences USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 20)
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_skills_embedding
                ON skills USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 10)
            """)

            # B-tree indexes for common filters
            cur.execute("CREATE INDEX IF NOT EXISTS idx_experiences_symbol ON experiences (symbol)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_experiences_exp_type ON experiences (exp_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_experiences_timestamp ON experiences (timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_skills_status ON skills (status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_skills_applies_to ON skills (applies_to)")

    # ── Experience Storage ──

    def store_experience(
        self,
        experience: Experience,
        embedding: list[float],
    ) -> str:
        """Store a trading experience with its embedding."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO experiences
                   (experience_id, timestamp, exp_type, context, decision, outcome, metadata, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (experience_id) DO UPDATE SET
                     context = EXCLUDED.context,
                     decision = EXCLUDED.decision,
                     outcome = EXCLUDED.outcome,
                     metadata = EXCLUDED.metadata,
                     embedding = EXCLUDED.embedding
                """,
                (
                    experience.experience_id,
                    experience.timestamp,
                    experience.exp_type,
                    json.dumps(experience.context, default=str),
                    json.dumps(experience.decision, default=str),
                    json.dumps(experience.outcome, default=str) if experience.outcome else None,
                    json.dumps(experience.metadata, default=str),
                    embedding,
                ),
            )

        logger.info(
            "experience_stored",
            extra={
                "experience_id": experience.experience_id,
                "type": experience.exp_type,
                "symbol": experience.context.get("symbol", ""),
            },
        )
        return experience.experience_id

    def query_similar(
        self,
        embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SimilarExperience]:
        """Find the most similar past experiences using cosine distance."""
        conn = self._get_conn()

        # Build WHERE clause from filters
        conditions = []
        params: list[Any] = [embedding, top_k]

        if filters:
            if signal := filters.get("signal"):
                conditions.append(f"signal = ${len(params) + 1}")
                params.append(signal)
            if exp_type := filters.get("exp_type"):
                conditions.append(f"exp_type = ${len(params) + 1}")
                params.append(exp_type)
            if filters.get("has_outcome"):
                conditions.append("has_outcome = TRUE")

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        # Use %s placeholders for psycopg3
        # Build the query with numbered placeholders manually
        filter_clauses = []
        filter_params: list[Any] = []

        if filters:
            if signal := filters.get("signal"):
                filter_clauses.append("signal = %s")
                filter_params.append(signal)
            if exp_type := filters.get("exp_type"):
                filter_clauses.append("exp_type = %s")
                filter_params.append(exp_type)
            if filters.get("has_outcome"):
                filter_clauses.append("has_outcome = TRUE")

        where_sql = ""
        if filter_clauses:
            where_sql = "WHERE " + " AND ".join(filter_clauses)

        query = f"""
            SELECT experience_id, timestamp, exp_type, context, decision,
                   outcome, metadata,
                   embedding <=> %s::vector AS distance
            FROM experiences
            {where_sql}
            ORDER BY distance ASC
            LIMIT %s
        """

        all_params = [embedding] + filter_params + [top_k]

        with conn.cursor() as cur:
            cur.execute(query, all_params)
            rows = cur.fetchall()

        similar = []
        for row in rows:
            try:
                exp = Experience(
                    experience_id=row[0],
                    timestamp=str(row[1]),
                    exp_type=row[2],
                    context=row[3] if isinstance(row[3], dict) else json.loads(row[3]),
                    decision=row[4] if isinstance(row[4], dict) else json.loads(row[4]),
                    outcome=(row[5] if isinstance(row[5], dict) else json.loads(row[5])) if row[5] else None,
                    metadata=row[6] if isinstance(row[6], dict) else json.loads(row[6] or "{}"),
                )
                distance = float(row[7])
                similar.append(SimilarExperience(
                    experience=exp,
                    distance=distance,
                    similarity=max(0, 1 - distance),
                ))
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                logger.warning("experience_parse_error", extra={"error": str(e)})

        return similar

    def backfill_outcome(
        self, experience_id: str, outcome: dict[str, Any], embedding: list[float]
    ) -> bool:
        """Update an experience with its outcome and re-embed."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE experiences
                   SET outcome = %s, embedding = %s
                   WHERE experience_id = %s""",
                (json.dumps(outcome, default=str), embedding, experience_id),
            )
            if cur.rowcount == 0:
                logger.warning("backfill_not_found", extra={"experience_id": experience_id})
                return False

        logger.info(
            "outcome_backfilled",
            extra={"experience_id": experience_id, "pnl_pct": outcome.get("pnl_pct")},
        )
        return True

    def get_experience(self, experience_id: str) -> Experience | None:
        """Retrieve a single experience by ID."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT experience_id, timestamp, exp_type, context, decision, outcome, metadata "
                "FROM experiences WHERE experience_id = %s",
                (experience_id,),
            )
            row = cur.fetchone()

        if not row:
            return None

        try:
            return Experience(
                experience_id=row[0],
                timestamp=str(row[1]),
                exp_type=row[2],
                context=row[3] if isinstance(row[3], dict) else json.loads(row[3]),
                decision=row[4] if isinstance(row[4], dict) else json.loads(row[4]),
                outcome=(row[5] if isinstance(row[5], dict) else json.loads(row[5])) if row[5] else None,
                metadata=row[6] if isinstance(row[6], dict) else json.loads(row[6] or "{}"),
            )
        except (json.JSONDecodeError, KeyError):
            return None

    def get_experiences_by_date(self, date: str) -> list[Experience]:
        """Get all experiences for a given date (YYYY-MM-DD)."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT experience_id, timestamp, exp_type, context, decision, outcome, metadata "
                "FROM experiences WHERE timestamp >= %s::timestamptz "
                "AND timestamp < (%s::date + INTERVAL '1 day')::timestamptz "
                "ORDER BY timestamp",
                (f"{date}T00:00:00Z", date),
            )
            rows = cur.fetchall()

        experiences = []
        for row in rows:
            try:
                experiences.append(Experience(
                    experience_id=row[0],
                    timestamp=str(row[1]),
                    exp_type=row[2],
                    context=row[3] if isinstance(row[3], dict) else json.loads(row[3]),
                    decision=row[4] if isinstance(row[4], dict) else json.loads(row[4]),
                    outcome=(row[5] if isinstance(row[5], dict) else json.loads(row[5])) if row[5] else None,
                    metadata=row[6] if isinstance(row[6], dict) else json.loads(row[6] or "{}"),
                ))
            except (json.JSONDecodeError, KeyError):
                continue
        return experiences

    # ── Skill Storage ──

    def save_skill(self, skill: Skill, embedding: list[float] | None = None) -> str:
        """Save or update a skill."""
        now = datetime.now(timezone.utc).isoformat()
        if not skill.created_at:
            skill.created_at = now
        skill.updated_at = now

        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO skills
                   (skill_id, title, rule, evidence_json, category, confidence,
                    status, effectiveness, applies_to, created_at, updated_at,
                    retired_at, retire_reason, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (skill_id) DO UPDATE SET
                     title = EXCLUDED.title,
                     rule = EXCLUDED.rule,
                     evidence_json = EXCLUDED.evidence_json,
                     category = EXCLUDED.category,
                     confidence = EXCLUDED.confidence,
                     status = EXCLUDED.status,
                     effectiveness = EXCLUDED.effectiveness,
                     applies_to = EXCLUDED.applies_to,
                     updated_at = EXCLUDED.updated_at,
                     retired_at = EXCLUDED.retired_at,
                     retire_reason = EXCLUDED.retire_reason,
                     embedding = EXCLUDED.embedding
                """,
                (
                    skill.skill_id,
                    skill.title,
                    skill.rule,
                    json.dumps(skill.evidence),
                    skill.category,
                    skill.confidence,
                    skill.status,
                    skill.effectiveness,
                    skill.applies_to,
                    skill.created_at,
                    skill.updated_at,
                    skill.retired_at,
                    skill.retire_reason,
                    embedding,
                ),
            )

        logger.info("skill_saved", extra={"skill_id": skill.skill_id, "status": skill.status})
        return skill.skill_id

    def get_active_skills(self, applies_to: str | None = None) -> list[Skill]:
        """Get all active skills, optionally filtered by applies_to."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            if applies_to:
                cur.execute(
                    "SELECT * FROM skills WHERE status='active' AND applies_to=%s ORDER BY confidence DESC",
                    (applies_to,),
                )
            else:
                cur.execute("SELECT * FROM skills WHERE status='active' ORDER BY confidence DESC")
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]

        return [self._row_to_skill(dict(zip(cols, r))) for r in rows]

    def get_skill(self, skill_id: str) -> Skill | None:
        """Get a single skill by ID."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM skills WHERE skill_id=%s", (skill_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d.name for d in cur.description]
        return self._row_to_skill(dict(zip(cols, row)))

    def retire_skill(self, skill_id: str, reason: str) -> bool:
        """Retire a skill (mark inactive with reason)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE skills SET status='retired', retired_at=%s, retire_reason=%s, updated_at=%s "
                "WHERE skill_id=%s",
                (now, reason, now, skill_id),
            )
            if cur.rowcount > 0:
                logger.info("skill_retired", extra={"skill_id": skill_id, "reason": reason})
                return True
        return False

    def query_relevant_skills(self, embedding: list[float], top_k: int = 5) -> list[Skill]:
        """Find skills most relevant to a given context (vector similarity on skill rules)."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM skills
                   WHERE status = 'active' AND embedding IS NOT NULL
                   ORDER BY embedding <=> %s::vector ASC
                   LIMIT %s""",
                (embedding, top_k),
            )
            rows = cur.fetchall()
            if not rows:
                return []
            cols = [d.name for d in cur.description]

        return [self._row_to_skill(dict(zip(cols, r))) for r in rows]

    def save_reflection(
        self, date: str, summary: dict, skills_created: int,
        skills_retired: int, experiences_analyzed: int,
    ) -> int:
        """Save an EOD reflection summary."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO reflections
                   (date, summary_json, skills_created, skills_retired, experiences_analyzed)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (date, json.dumps(summary, default=str), skills_created, skills_retired, experiences_analyzed),
            )
            row = cur.fetchone()
        return row[0] if row else 0

    def get_stats(self) -> dict[str, Any]:
        """Return brain health stats."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM experiences")
            exp_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM skills")
            skill_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM skills WHERE status='active'")
            active_skills = cur.fetchone()[0]

            cur.execute("SELECT AVG(confidence) FROM skills WHERE status='active'")
            avg_confidence = cur.fetchone()[0]

            cur.execute(
                "SELECT AVG(effectiveness) FROM skills WHERE status='active' AND effectiveness IS NOT NULL"
            )
            avg_effectiveness = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM reflections")
            reflection_count = cur.fetchone()[0]

        return {
            "total_experiences": exp_count,
            "total_skills": skill_count,
            "active_skills": active_skills,
            "avg_confidence": round(avg_confidence, 3) if avg_confidence else None,
            "avg_effectiveness": round(avg_effectiveness, 3) if avg_effectiveness else None,
            "total_reflections": reflection_count,
        }

    @staticmethod
    def _row_to_skill(row: dict) -> Skill:
        return Skill(
            skill_id=row["skill_id"],
            title=row["title"],
            rule=row["rule"],
            evidence=json.loads(row["evidence_json"]),
            category=row["category"],
            confidence=row["confidence"],
            status=row["status"],
            effectiveness=row["effectiveness"],
            applies_to=row["applies_to"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            retired_at=str(row["retired_at"]) if row.get("retired_at") else None,
            retire_reason=row.get("retire_reason"),
        )

    def close(self) -> None:
        """Close the database connection."""
        if not self._conn.closed:
            self._conn.close()
