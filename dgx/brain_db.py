"""Brain DB — ChromaDB experience store + SQLite skill store.

Two storage layers:
1. ChromaDB: Vector store for trading experiences (embed + similarity search)
2. SQLite: Structured store for skills (rules learned from experience clusters)

Both persist to disk on the DGX.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb

logger = logging.getLogger("brain.db")

# Default persistence paths on DGX
DEFAULT_CHROMA_PATH = "/home/sankerkr/brain_data/chromadb"
DEFAULT_SQLITE_PATH = "/home/sankerkr/brain_data/skills.sqlite3"

EXPERIENCES_COLLECTION = "trading_experiences"
SKILLS_COLLECTION = "trading_skills"


@dataclass
class Experience:
    """A single trading experience — decision + context + outcome."""

    experience_id: str
    timestamp: str
    exp_type: str  # entry_decision, exit_decision, denial, session_adaptation
    context: dict[str, Any]
    decision: dict[str, Any]
    outcome: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    """A learned trading rule distilled from experience patterns."""

    skill_id: str
    title: str
    rule: str
    evidence: list[str]  # experience_ids
    category: str  # entry_filter, sizing, timing, exit, risk
    confidence: float
    status: str  # active, retired, suspended, pending_verification
    effectiveness: float | None = None
    applies_to: str = "pm_entry_approval"  # which agent/prompt
    created_at: str = ""
    updated_at: str = ""
    retired_at: str | None = None
    retire_reason: str | None = None


@dataclass
class SimilarExperience:
    """Result from a similarity search — experience + distance score."""

    experience: Experience
    distance: float
    similarity: float  # 1 - distance (higher = more similar)


class BrainDB:
    """Combined ChromaDB + SQLite storage for the Trading Brain."""

    def __init__(
        self,
        chroma_path: str = DEFAULT_CHROMA_PATH,
        sqlite_path: str = DEFAULT_SQLITE_PATH,
    ):
        # Ensure directories exist
        Path(chroma_path).mkdir(parents=True, exist_ok=True)
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)

        # ChromaDB — persistent client
        logger.info("initializing_chromadb", extra={"path": chroma_path})
        self.chroma = chromadb.PersistentClient(path=chroma_path)
        self.experiences = self.chroma.get_or_create_collection(
            name=EXPERIENCES_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
        self.skills_vectors = self.chroma.get_or_create_collection(
            name=SKILLS_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

        # SQLite — skill structured data
        self.sqlite_path = sqlite_path
        self._init_sqlite()

        exp_count = self.experiences.count()
        skill_count = self._count_skills()
        logger.info(
            "brain_db_ready",
            extra={
                "chroma_path": chroma_path,
                "sqlite_path": sqlite_path,
                "experience_count": exp_count,
                "skill_count": skill_count,
            },
        )

    def _init_sqlite(self) -> None:
        """Create skills table if it doesn't exist."""
        conn = sqlite3.connect(self.sqlite_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                skill_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                rule TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                category TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'pending_verification',
                effectiveness REAL,
                applies_to TEXT NOT NULL DEFAULT 'pm_entry_approval',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                retired_at TEXT,
                retire_reason TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                skills_created INTEGER DEFAULT 0,
                skills_retired INTEGER DEFAULT 0,
                experiences_analyzed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def _count_skills(self) -> int:
        conn = sqlite3.connect(self.sqlite_path)
        row = conn.execute("SELECT COUNT(*) FROM skills").fetchone()
        conn.close()
        return row[0] if row else 0

    # ── Experience Storage ──

    def store_experience(
        self,
        experience: Experience,
        embedding: list[float],
    ) -> str:
        """Store a trading experience with its embedding in ChromaDB."""
        # Build metadata for ChromaDB filtering
        meta = {
            "exp_type": experience.exp_type,
            "timestamp": experience.timestamp,
            "symbol": experience.context.get("symbol", ""),
            "signal": experience.context.get("signal", ""),
            "sentiment": experience.context.get("sentiment", ""),
            "sector": experience.context.get("sector", ""),
            "action": experience.decision.get("action", ""),
            "has_outcome": experience.outcome is not None,
        }

        # Store full experience as document JSON
        doc = json.dumps(asdict(experience), default=str)

        self.experiences.upsert(
            ids=[experience.experience_id],
            embeddings=[embedding],
            documents=[doc],
            metadatas=[meta],
        )

        logger.info(
            "experience_stored",
            extra={
                "experience_id": experience.experience_id,
                "type": experience.exp_type,
                "symbol": meta["symbol"],
            },
        )
        return experience.experience_id

    def query_similar(
        self,
        embedding: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SimilarExperience]:
        """Find the most similar past experiences."""
        where = None
        if filters:
            # Build ChromaDB where clause
            conditions = []
            if signal := filters.get("signal"):
                conditions.append({"signal": signal})
            if exp_type := filters.get("exp_type"):
                conditions.append({"exp_type": exp_type})
            if filters.get("has_outcome"):
                conditions.append({"has_outcome": True})
            if len(conditions) == 1:
                where = conditions[0]
            elif len(conditions) > 1:
                where = {"$and": conditions}

        results = self.experiences.query(
            query_embeddings=[embedding],
            n_results=min(top_k, self.experiences.count() or 1),
            where=where,
            include=["documents", "distances", "metadatas"],
        )

        similar = []
        if results and results["ids"] and results["ids"][0]:
            for i, exp_id in enumerate(results["ids"][0]):
                doc_json = results["documents"][0][i]
                distance = results["distances"][0][i]
                try:
                    data = json.loads(doc_json)
                    exp = Experience(
                        experience_id=data["experience_id"],
                        timestamp=data["timestamp"],
                        exp_type=data["exp_type"],
                        context=data["context"],
                        decision=data["decision"],
                        outcome=data.get("outcome"),
                        metadata=data.get("metadata", {}),
                    )
                    similar.append(SimilarExperience(
                        experience=exp,
                        distance=distance,
                        similarity=max(0, 1 - distance),
                    ))
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning("experience_parse_error", extra={"id": exp_id, "error": str(e)})

        return similar

    def backfill_outcome(self, experience_id: str, outcome: dict[str, Any], embedding: list[float]) -> bool:
        """Update an experience with its outcome and re-embed."""
        # Get current document
        result = self.experiences.get(ids=[experience_id], include=["documents", "metadatas"])
        if not result or not result["ids"]:
            logger.warning("backfill_not_found", extra={"experience_id": experience_id})
            return False

        doc_json = result["documents"][0]
        meta = result["metadatas"][0]

        try:
            data = json.loads(doc_json)
        except json.JSONDecodeError:
            return False

        data["outcome"] = outcome
        meta["has_outcome"] = True
        if pnl := outcome.get("pnl_pct"):
            meta["outcome_positive"] = pnl > 0

        self.experiences.update(
            ids=[experience_id],
            embeddings=[embedding],
            documents=[json.dumps(data, default=str)],
            metadatas=[meta],
        )
        logger.info("outcome_backfilled", extra={"experience_id": experience_id, "pnl_pct": outcome.get("pnl_pct")})
        return True

    def get_experience(self, experience_id: str) -> Experience | None:
        """Retrieve a single experience by ID."""
        result = self.experiences.get(ids=[experience_id], include=["documents"])
        if not result or not result["ids"]:
            return None
        try:
            data = json.loads(result["documents"][0])
            return Experience(**{k: data[k] for k in Experience.__dataclass_fields__ if k in data})
        except (json.JSONDecodeError, KeyError):
            return None

    def get_experiences_by_date(self, date: str) -> list[Experience]:
        """Get all experiences for a given date (YYYY-MM-DD)."""
        results = self.experiences.get(
            where={"timestamp": {"$gte": f"{date}T00:00:00"}},
            include=["documents"],
        )
        experiences = []
        if results and results["documents"]:
            for doc_json in results["documents"]:
                try:
                    data = json.loads(doc_json)
                    experiences.append(Experience(**{k: data[k] for k in Experience.__dataclass_fields__ if k in data}))
                except (json.JSONDecodeError, KeyError):
                    continue
        return experiences

    def list_recent_experiences(self, limit: int = 20) -> list[Experience]:
        """Return recent experiences ordered by timestamp descending."""
        results = self.experiences.get(include=["documents"])
        experiences: list[Experience] = []
        if results and results["documents"]:
            for doc_json in results["documents"]:
                try:
                    data = json.loads(doc_json)
                    experiences.append(
                        Experience(
                            **{
                                k: data[k]
                                for k in Experience.__dataclass_fields__
                                if k in data
                            }
                        )
                    )
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    logger.warning("experience_parse_error", extra={"error": str(exc)})
        experiences.sort(key=lambda exp: exp.timestamp, reverse=True)
        return experiences[: max(1, limit)]

    # ── Skill Storage ──

    def save_skill(self, skill: Skill, embedding: list[float] | None = None) -> str:
        """Save or update a skill in SQLite and optionally in ChromaDB."""
        now = datetime.now(timezone.utc).isoformat()
        if not skill.created_at:
            skill.created_at = now
        skill.updated_at = now

        conn = sqlite3.connect(self.sqlite_path)
        conn.execute(
            """INSERT OR REPLACE INTO skills
               (skill_id, title, rule, evidence_json, category, confidence,
                status, effectiveness, applies_to, created_at, updated_at,
                retired_at, retire_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        conn.commit()
        conn.close()

        # Also store skill embedding for retrieval alongside experiences
        if embedding:
            self.skills_vectors.upsert(
                ids=[skill.skill_id],
                embeddings=[embedding],
                documents=[json.dumps({"skill_id": skill.skill_id, "rule": skill.rule, "category": skill.category})],
                metadatas={"status": skill.status, "category": skill.category, "applies_to": skill.applies_to},
            )

        logger.info("skill_saved", extra={"skill_id": skill.skill_id, "status": skill.status})
        return skill.skill_id

    def get_active_skills(self, applies_to: str | None = None) -> list[Skill]:
        """Get all active skills, optionally filtered by applies_to."""
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        if applies_to:
            rows = conn.execute(
                "SELECT * FROM skills WHERE status='active' AND applies_to=? ORDER BY confidence DESC",
                (applies_to,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM skills WHERE status='active' ORDER BY confidence DESC"
            ).fetchall()
        conn.close()
        return [self._row_to_skill(r) for r in rows]

    def get_skill(self, skill_id: str) -> Skill | None:
        """Get a single skill by ID."""
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM skills WHERE skill_id=?", (skill_id,)).fetchone()
        conn.close()
        return self._row_to_skill(row) if row else None

    def retire_skill(self, skill_id: str, reason: str) -> bool:
        """Retire a skill (mark inactive with reason)."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.sqlite_path)
        cursor = conn.execute(
            "UPDATE skills SET status='retired', retired_at=?, retire_reason=?, updated_at=? WHERE skill_id=?",
            (now, reason, now, skill_id),
        )
        conn.commit()
        conn.close()
        if cursor.rowcount > 0:
            logger.info("skill_retired", extra={"skill_id": skill_id, "reason": reason})
            return True
        return False

    def query_relevant_skills(self, embedding: list[float], top_k: int = 5) -> list[Skill]:
        """Find skills most relevant to a given context (vector search on skill rules)."""
        count = self.skills_vectors.count()
        if count == 0:
            return []

        results = self.skills_vectors.query(
            query_embeddings=[embedding],
            n_results=min(top_k, count),
            where={"status": "active"},
            include=["documents", "metadatas"],
        )

        skills = []
        if results and results["ids"] and results["ids"][0]:
            for doc_json in results["documents"][0]:
                try:
                    data = json.loads(doc_json)
                    skill = self.get_skill(data["skill_id"])
                    if skill:
                        skills.append(skill)
                except (json.JSONDecodeError, KeyError):
                    continue
        return skills

    def save_reflection(self, date: str, summary: dict, skills_created: int, skills_retired: int, experiences_analyzed: int) -> int:
        """Save an EOD reflection summary."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.sqlite_path)
        cursor = conn.execute(
            """INSERT INTO reflections (date, summary_json, skills_created, skills_retired, experiences_analyzed, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (date, json.dumps(summary, default=str), skills_created, skills_retired, experiences_analyzed, now),
        )
        conn.commit()
        reflection_id = cursor.lastrowid
        conn.close()
        return reflection_id

    def list_reflections(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent reflection summaries."""
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, date, summary_json, skills_created, skills_retired, "
            "experiences_analyzed, created_at FROM reflections "
            "ORDER BY date DESC, created_at DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        conn.close()
        reflections: list[dict[str, Any]] = []
        for row in rows:
            try:
                summary = json.loads(row["summary_json"] or "{}")
            except json.JSONDecodeError as exc:
                logger.warning("reflection_parse_error", extra={"error": str(exc)})
                summary = {}
            reflections.append(
                {
                    "id": row["id"],
                    "date": row["date"],
                    "summary": summary,
                    "skills_created": row["skills_created"],
                    "skills_retired": row["skills_retired"],
                    "experiences_analyzed": row["experiences_analyzed"],
                    "created_at": row["created_at"],
                }
            )
        return reflections

    def get_stats(self) -> dict[str, Any]:
        """Return brain health stats."""
        exp_count = self.experiences.count()
        skill_count = self._count_skills()

        conn = sqlite3.connect(self.sqlite_path)
        active_skills = conn.execute("SELECT COUNT(*) FROM skills WHERE status='active'").fetchone()[0]
        avg_confidence = conn.execute("SELECT AVG(confidence) FROM skills WHERE status='active'").fetchone()[0]
        avg_effectiveness = conn.execute("SELECT AVG(effectiveness) FROM skills WHERE status='active' AND effectiveness IS NOT NULL").fetchone()[0]
        reflection_count = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        conn.close()

        return {
            "total_experiences": exp_count,
            "total_skills": skill_count,
            "active_skills": active_skills,
            "avg_confidence": round(avg_confidence, 3) if avg_confidence else None,
            "avg_effectiveness": round(avg_effectiveness, 3) if avg_effectiveness else None,
            "total_reflections": reflection_count,
        }

    @staticmethod
    def _row_to_skill(row: sqlite3.Row) -> Skill:
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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            retired_at=row["retired_at"],
            retire_reason=row["retire_reason"],
        )
