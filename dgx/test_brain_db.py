"""Tests for BrainDB backends — verifies ChromaDB and PgVector have identical interfaces.

Run:
    # ChromaDB (default, no external deps):
    cd dgx && python -m pytest test_brain_db.py -v

    # PgVector (requires running PostgreSQL):
    BRAIN_DB_BACKEND=pgvector BRAIN_PG_DSN=postgresql://brain:brain@localhost:5432/brain_test \
        python -m pytest test_brain_db.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest

from brain_db import BrainDB, Experience, Skill, SimilarExperience

BACKEND = os.getenv("BRAIN_DB_BACKEND", "chroma")
EMBEDDING_DIM = 384


def _random_embedding() -> list[float]:
    """Generate a random normalized embedding."""
    import random
    raw = [random.gauss(0, 1) for _ in range(EMBEDDING_DIM)]
    norm = sum(x * x for x in raw) ** 0.5
    return [x / norm for x in raw]


def _similar_embedding(base: list[float], noise: float = 0.1) -> list[float]:
    """Generate an embedding similar to base with some noise."""
    import random
    raw = [x + random.gauss(0, noise) for x in base]
    norm = sum(x * x for x in raw) ** 0.5
    return [x / norm for x in raw]


@pytest.fixture
def db():
    """Create a fresh DB instance for testing."""
    if BACKEND == "pgvector":
        from brain_db_pgvector import PgVectorBrainDB
        dsn = os.getenv("BRAIN_PG_DSN", "postgresql://brain:brain@localhost:5432/brain_test")
        _db = PgVectorBrainDB(dsn=dsn)
        # Clean tables for a fresh test
        conn = _db._get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reflections")
            cur.execute("DELETE FROM skills")
            cur.execute("DELETE FROM experiences")
        yield _db
        _db.close()
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            chroma_path = str(Path(tmpdir) / "chroma")
            sqlite_path = str(Path(tmpdir) / "skills.sqlite3")
            _db = BrainDB(chroma_path=chroma_path, sqlite_path=sqlite_path)
            yield _db


class TestExperienceStorage:
    def test_store_and_retrieve(self, db):
        exp = Experience(
            experience_id="test-exp-001",
            timestamp="2026-05-14T10:00:00Z",
            exp_type="entry_decision",
            context={"symbol": "AAPL", "signal": "volume_spike", "sentiment": "positive"},
            decision={"action": "approve", "reasoning": "High RVOL with positive catalyst"},
        )
        emb = _random_embedding()
        stored_id = db.store_experience(exp, emb)
        assert stored_id == "test-exp-001"

        retrieved = db.get_experience("test-exp-001")
        assert retrieved is not None
        assert retrieved.experience_id == "test-exp-001"
        assert retrieved.context["symbol"] == "AAPL"
        assert retrieved.decision["action"] == "approve"

    def test_get_nonexistent(self, db):
        assert db.get_experience("nonexistent-id") is None

    def test_backfill_outcome(self, db):
        exp = Experience(
            experience_id="test-backfill-001",
            timestamp="2026-05-14T10:00:00Z",
            exp_type="entry_decision",
            context={"symbol": "MSFT", "signal": "analyst_target_raise"},
            decision={"action": "approve"},
        )
        emb = _random_embedding()
        db.store_experience(exp, emb)

        outcome = {"pnl_pct": 1.5, "exit_reason": "TARGET", "hold_minutes": 25}
        new_emb = _random_embedding()
        success = db.backfill_outcome("test-backfill-001", outcome, new_emb)
        assert success

        updated = db.get_experience("test-backfill-001")
        assert updated.outcome is not None
        assert updated.outcome["pnl_pct"] == 1.5

    def test_backfill_nonexistent(self, db):
        assert not db.backfill_outcome("no-such-id", {"pnl": 0}, _random_embedding())

    def test_query_similar(self, db):
        base_emb = _random_embedding()

        # Store 5 experiences with similar embeddings
        for i in range(5):
            exp = Experience(
                experience_id=f"sim-{i}",
                timestamp=f"2026-05-14T1{i}:00:00Z",
                exp_type="entry_decision",
                context={"symbol": f"SYM{i}", "signal": "volume_spike"},
                decision={"action": "approve"},
            )
            emb = _similar_embedding(base_emb, noise=0.05 * (i + 1))
            db.store_experience(exp, emb)

        # Query should return experiences sorted by similarity
        results = db.query_similar(base_emb, top_k=3)
        assert len(results) <= 3
        assert all(isinstance(r, SimilarExperience) for r in results)
        # Should be sorted by distance (ascending) / similarity (descending)
        if len(results) >= 2:
            assert results[0].similarity >= results[1].similarity


class TestSkillStorage:
    def test_save_and_retrieve(self, db):
        skill = Skill(
            skill_id="skill-test-001",
            title="Deny 3rd same-symbol entry",
            rule="If a symbol has been traded 2+ times today, deny further entries",
            evidence=["exp-1", "exp-2"],
            category="entry_filter",
            confidence=0.8,
            status="active",
            applies_to="pm_entry_approval",
        )
        emb = _random_embedding()
        db.save_skill(skill, embedding=emb)

        retrieved = db.get_skill("skill-test-001")
        assert retrieved is not None
        assert retrieved.title == "Deny 3rd same-symbol entry"
        assert retrieved.confidence == 0.8
        assert retrieved.status == "active"
        assert "exp-1" in retrieved.evidence

    def test_get_active_skills(self, db):
        for i in range(3):
            skill = Skill(
                skill_id=f"skill-active-{i}",
                title=f"Rule {i}",
                rule=f"Test rule {i}",
                evidence=[],
                category="entry_filter",
                confidence=0.5 + i * 0.1,
                status="active",
                applies_to="pm_entry_approval",
            )
            db.save_skill(skill)

        # Add a retired skill
        retired = Skill(
            skill_id="skill-retired-1",
            title="Old rule",
            rule="Obsolete",
            evidence=[],
            category="entry_filter",
            confidence=0.3,
            status="retired",
            applies_to="pm_entry_approval",
        )
        db.save_skill(retired)

        active = db.get_active_skills()
        assert len(active) >= 3
        assert all(s.status == "active" for s in active)
        # Should be sorted by confidence descending
        if len(active) >= 2:
            assert active[0].confidence >= active[1].confidence

    def test_retire_skill(self, db):
        skill = Skill(
            skill_id="skill-to-retire",
            title="Will be retired",
            rule="Temporary rule",
            evidence=[],
            category="risk",
            confidence=0.6,
            status="active",
        )
        db.save_skill(skill)

        success = db.retire_skill("skill-to-retire", "Low effectiveness")
        assert success

        updated = db.get_skill("skill-to-retire")
        assert updated.status == "retired"
        assert updated.retire_reason == "Low effectiveness"

    def test_retire_nonexistent(self, db):
        assert not db.retire_skill("no-such-skill", "test")

    def test_query_relevant_skills(self, db):
        base_emb = _random_embedding()

        for i in range(3):
            skill = Skill(
                skill_id=f"skill-rel-{i}",
                title=f"Relevant rule {i}",
                rule=f"Rule for volume spike entries {i}",
                evidence=[],
                category="entry_filter",
                confidence=0.7,
                status="active",
            )
            emb = _similar_embedding(base_emb, noise=0.05 * (i + 1))
            db.save_skill(skill, embedding=emb)

        results = db.query_relevant_skills(base_emb, top_k=2)
        assert len(results) <= 2
        assert all(isinstance(s, Skill) for s in results)


class TestReflections:
    def test_save_reflection(self, db):
        summary = {"date": "2026-05-14", "winners": 3, "losers": 1}
        ref_id = db.save_reflection("2026-05-14", summary, skills_created=2, skills_retired=1, experiences_analyzed=10)
        assert ref_id > 0 or ref_id is not None


class TestStats:
    def test_stats_structure(self, db):
        stats = db.get_stats()
        assert "total_experiences" in stats
        assert "total_skills" in stats
        assert "active_skills" in stats
        assert "avg_confidence" in stats
        assert "avg_effectiveness" in stats
        assert "total_reflections" in stats


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
