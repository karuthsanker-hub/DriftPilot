"""Brain Server — FastAPI service for the PM Trading Brain.

Runs on DGX Spark at port 8100 alongside vLLM (port 8000).
Provides experience storage, similarity search, skill management,
and end-of-day reflection via Qwen.

Usage:
    uvicorn brain_server:app --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from brain_db import BrainDB, Experience, Skill, SimilarExperience
from brain_embedder import BrainEmbedder

logger = logging.getLogger("brain.server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── Configuration ──

BRAIN_DB_BACKEND = os.getenv("BRAIN_DB_BACKEND", "chroma")  # "chroma" or "pgvector"
CHROMA_PATH = os.getenv("BRAIN_CHROMA_PATH", "/home/sankerkr/brain_data/chromadb")
SQLITE_PATH = os.getenv("BRAIN_SQLITE_PATH", "/home/sankerkr/brain_data/skills.sqlite3")
PG_DSN = os.getenv("BRAIN_PG_DSN", "postgresql://brain:brain@localhost:5432/brain")
EMBEDDING_MODEL = os.getenv("BRAIN_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
EMBEDDING_DEVICE = os.getenv("BRAIN_EMBEDDING_DEVICE", "cpu")
QWEN_URL = os.getenv("BRAIN_QWEN_URL", "http://localhost:8000/v1/chat/completions")
QWEN_MODEL = os.getenv("BRAIN_QWEN_MODEL", "Qwen/Qwen3-8B")

# Globals initialized at startup
embedder: BrainEmbedder | None = None
db: BrainDB | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize embedder and DB on startup."""
    global embedder, db
    logger.info("brain_server_starting", extra={"backend": BRAIN_DB_BACKEND})
    embedder = BrainEmbedder(model_name=EMBEDDING_MODEL, device=EMBEDDING_DEVICE)

    if BRAIN_DB_BACKEND == "pgvector":
        from brain_db_pgvector import PgVectorBrainDB
        db = PgVectorBrainDB(dsn=PG_DSN)
        logger.info("brain_server_ready", extra={"backend": "pgvector", "dsn": PG_DSN.split("@")[-1]})
    else:
        db = BrainDB(chroma_path=CHROMA_PATH, sqlite_path=SQLITE_PATH)
        logger.info("brain_server_ready", extra={"backend": "chroma", "chroma": CHROMA_PATH, "sqlite": SQLITE_PATH})

    yield
    if BRAIN_DB_BACKEND == "pgvector" and hasattr(db, "close"):
        db.close()
    logger.info("brain_server_shutting_down")


app = FastAPI(title="PM Trading Brain", lifespan=lifespan)


# ── Request/Response Models ──

class StoreRequest(BaseModel):
    """Store a new trading experience."""
    experience_id: str | None = None
    timestamp: str | None = None
    exp_type: str = "entry_decision"
    context: dict[str, Any]
    decision: dict[str, Any]
    outcome: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryRequest(BaseModel):
    """Query for similar past experiences."""
    context: dict[str, Any]
    top_k: int = 5
    filters: dict[str, Any] | None = None
    include_skills: bool = True


class BackfillRequest(BaseModel):
    """Backfill an experience with its outcome."""
    experience_id: str
    outcome: dict[str, Any]


class ReflectRequest(BaseModel):
    """Trigger end-of-day reflection."""
    date: str  # YYYY-MM-DD


class SkillCreate(BaseModel):
    """Manually create a skill (for bootstrap)."""
    title: str
    rule: str
    evidence: list[str] = Field(default_factory=list)
    category: str = "entry_filter"
    confidence: float = 0.5
    applies_to: str = "pm_entry_approval"
    source: str = "manual"


class BulkStoreRequest(BaseModel):
    """Bulk store experiences (for bootstrap from historical data)."""
    experiences: list[StoreRequest]


# ── Endpoints ──

@app.get("/brain/health")
def health():
    """Health check."""
    return {
        "ok": True,
        "service": "pm-trading-brain",
        "backend": BRAIN_DB_BACKEND,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": embedder.dimension if embedder else None,
    }


@app.get("/brain/stats")
def stats():
    """Brain health statistics."""
    if not db:
        raise HTTPException(status_code=503, detail="Brain DB not initialized")
    return db.get_stats()


@app.post("/brain/store")
def store_experience(req: StoreRequest):
    """Store a single trading experience."""
    if not db or not embedder:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    exp_id = req.experience_id or f"exp-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ts = req.timestamp or datetime.now(timezone.utc).isoformat()

    experience = Experience(
        experience_id=exp_id,
        timestamp=ts,
        exp_type=req.exp_type,
        context=req.context,
        decision=req.decision,
        outcome=req.outcome,
        metadata=req.metadata,
    )

    # Build embedding text that includes outcome if available
    embed_text = embedder.context_to_text(req.context)
    if req.outcome:
        pnl = req.outcome.get("pnl_pct", 0)
        exit_reason = req.outcome.get("exit_reason", "unknown")
        embed_text += f" | Outcome: {pnl:+.1f}% ({exit_reason})"

    embedding = embedder.model.encode(embed_text, normalize_embeddings=True).tolist()
    stored_id = db.store_experience(experience, embedding)

    return {"ok": True, "experience_id": stored_id}


@app.post("/brain/store/bulk")
def store_bulk(req: BulkStoreRequest):
    """Bulk store experiences (for bootstrap)."""
    if not db or not embedder:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    stored = 0
    errors = 0
    for item in req.experiences:
        try:
            exp_id = item.experience_id or f"exp-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
            ts = item.timestamp or datetime.now(timezone.utc).isoformat()

            experience = Experience(
                experience_id=exp_id,
                timestamp=ts,
                exp_type=item.exp_type,
                context=item.context,
                decision=item.decision,
                outcome=item.outcome,
                metadata=item.metadata,
            )

            embed_text = embedder.context_to_text(item.context)
            if item.outcome:
                pnl = item.outcome.get("pnl_pct", 0)
                exit_reason = item.outcome.get("exit_reason", "unknown")
                embed_text += f" | Outcome: {pnl:+.1f}% ({exit_reason})"

            embedding = embedder.model.encode(embed_text, normalize_embeddings=True).tolist()
            db.store_experience(experience, embedding)
            stored += 1
        except Exception as e:
            errors += 1
            logger.warning("bulk_store_error", extra={"error": str(e)})

    return {"ok": True, "stored": stored, "errors": errors, "total": len(req.experiences)}


@app.post("/brain/query")
def query_similar(req: QueryRequest):
    """Query for similar past experiences + relevant skills."""
    if not db or not embedder:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    start = time.perf_counter()

    # Embed the current context
    embedding = embedder.embed_context(req.context)

    # Find similar experiences
    similar = db.query_similar(embedding, top_k=req.top_k, filters=req.filters)

    # Find relevant skills
    skills = []
    if req.include_skills:
        skills = db.query_relevant_skills(embedding, top_k=5)
        # Also include all active skills (they're small enough to always include)
        active_skills = db.get_active_skills(applies_to="pm_entry_approval")
        # Merge: vector-matched skills first, then remaining active skills
        seen_ids = {s.skill_id for s in skills}
        for s in active_skills:
            if s.skill_id not in seen_ids:
                skills.append(s)

    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

    return {
        "experiences": [
            {
                "experience_id": s.experience.experience_id,
                "similarity": round(s.similarity, 3),
                "context": s.experience.context,
                "decision": s.experience.decision,
                "outcome": s.experience.outcome,
                "timestamp": s.experience.timestamp,
            }
            for s in similar
        ],
        "skills": [
            {
                "skill_id": s.skill_id,
                "title": s.title,
                "rule": s.rule,
                "category": s.category,
                "confidence": s.confidence,
                "effectiveness": s.effectiveness,
            }
            for s in skills
        ],
        "query_ms": elapsed_ms,
        "experience_count": len(similar),
        "skill_count": len(skills),
    }


@app.post("/brain/backfill")
def backfill_outcome(req: BackfillRequest):
    """Backfill an experience with its outcome (after position closes)."""
    if not db or not embedder:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    # Get the existing experience to rebuild embedding with outcome
    exp = db.get_experience(req.experience_id)
    if not exp:
        raise HTTPException(status_code=404, detail=f"Experience {req.experience_id} not found")

    # Re-embed with outcome info
    embed_text = embedder.context_to_text(exp.context)
    pnl = req.outcome.get("pnl_pct", 0)
    exit_reason = req.outcome.get("exit_reason", "unknown")
    embed_text += f" | Outcome: {pnl:+.1f}% ({exit_reason})"
    embedding = embedder.model.encode(embed_text, normalize_embeddings=True).tolist()

    success = db.backfill_outcome(req.experience_id, req.outcome, embedding)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to backfill")

    return {"ok": True, "experience_id": req.experience_id}


@app.get("/brain/skills")
def list_skills(status: str = "active", applies_to: str | None = None):
    """List skills, optionally filtered."""
    if not db:
        raise HTTPException(status_code=503, detail="Brain DB not initialized")

    if status == "active":
        skills = db.get_active_skills(applies_to=applies_to)
    else:
        # Get all skills from SQLite
        import sqlite3
        conn = sqlite3.connect(db.sqlite_path)
        conn.row_factory = sqlite3.Row
        if applies_to:
            rows = conn.execute("SELECT * FROM skills WHERE applies_to=? ORDER BY updated_at DESC", (applies_to,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM skills ORDER BY updated_at DESC").fetchall()
        conn.close()
        skills = [db._row_to_skill(r) for r in rows]

    return {
        "skills": [asdict(s) for s in skills],
        "count": len(skills),
    }


@app.post("/brain/skills/create")
def create_skill(req: SkillCreate):
    """Manually create a skill (for bootstrap from defects/playbook)."""
    if not db or not embedder:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    skill_id = f"skill-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    skill = Skill(
        skill_id=skill_id,
        title=req.title,
        rule=req.rule,
        evidence=req.evidence,
        category=req.category,
        confidence=req.confidence,
        status="active",
        applies_to=req.applies_to,
        created_at=now,
        updated_at=now,
    )

    # Embed the skill rule for vector retrieval
    embedding = embedder.model.encode(req.rule, normalize_embeddings=True).tolist()
    db.save_skill(skill, embedding=embedding)

    return {"ok": True, "skill_id": skill_id}


@app.post("/brain/skills/{skill_id}/retire")
def retire_skill(skill_id: str, reason: str = "manual"):
    """Retire a skill."""
    if not db:
        raise HTTPException(status_code=503, detail="Brain DB not initialized")
    success = db.retire_skill(skill_id, reason)
    if not success:
        raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")
    return {"ok": True, "skill_id": skill_id, "status": "retired"}


@app.get("/brain/experience/{experience_id}")
def get_experience(experience_id: str):
    """Get a single experience by ID."""
    if not db:
        raise HTTPException(status_code=503, detail="Brain DB not initialized")
    exp = db.get_experience(experience_id)
    if not exp:
        raise HTTPException(status_code=404, detail=f"Experience {experience_id} not found")
    return asdict(exp)


@app.get("/brain/similar/{experience_id}")
def find_similar(experience_id: str, top_k: int = 5):
    """Find experiences similar to a given one (for debugging/exploration)."""
    if not db or not embedder:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    exp = db.get_experience(experience_id)
    if not exp:
        raise HTTPException(status_code=404, detail=f"Experience {experience_id} not found")

    embedding = embedder.embed_context(exp.context)
    similar = db.query_similar(embedding, top_k=top_k + 1)  # +1 because it'll find itself

    # Filter out self
    results = [s for s in similar if s.experience.experience_id != experience_id][:top_k]

    return {
        "source": experience_id,
        "similar": [
            {
                "experience_id": s.experience.experience_id,
                "similarity": round(s.similarity, 3),
                "symbol": s.experience.context.get("symbol"),
                "signal": s.experience.context.get("signal"),
                "action": s.experience.decision.get("action"),
                "outcome": s.experience.outcome,
            }
            for s in results
        ],
    }


@app.post("/brain/reflect")
async def reflect(req: ReflectRequest):
    """Run end-of-day reflection — analyze day's experiences, generate/verify skills.

    This calls Qwen to analyze patterns and generate new skills.
    """
    if not db or not embedder:
        raise HTTPException(status_code=503, detail="Brain not initialized")

    # Get all experiences for the date
    experiences = db.get_experiences_by_date(req.date)
    if not experiences:
        return {"status": "no_experiences", "message": f"No experiences found for {req.date}"}

    # Separate by outcome
    winners = [e for e in experiences if e.outcome and e.outcome.get("pnl_pct", 0) > 0]
    losers = [e for e in experiences if e.outcome and e.outcome.get("pnl_pct", 0) < 0]
    no_outcome = [e for e in experiences if not e.outcome]

    # Build reflection prompt for Qwen
    reflection_prompt = _build_reflection_prompt(experiences, winners, losers)

    # Call Qwen for pattern analysis
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                QWEN_URL,
                json={
                    "model": QWEN_MODEL,
                    "messages": [
                        {"role": "system", "content": REFLECTION_SYSTEM_PROMPT},
                        {"role": "user", "content": reflection_prompt + " /no_think"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000,
                },
            )
            response.raise_for_status()
            qwen_response = response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("reflection_qwen_error", extra={"error": str(e)})
        return {"status": "error", "message": f"Qwen reflection failed: {str(e)}"}

    # Parse skills from Qwen response
    new_skills = _parse_skills_from_reflection(qwen_response, experiences)
    skills_created = 0
    for skill_data in new_skills:
        skill = Skill(
            skill_id=f"skill-{uuid.uuid4().hex[:8]}",
            title=skill_data.get("title", "Untitled"),
            rule=skill_data.get("rule", ""),
            evidence=skill_data.get("evidence", []),
            category=skill_data.get("category", "entry_filter"),
            confidence=skill_data.get("confidence", 0.5),
            status="active",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        embedding = embedder.model.encode(skill.rule, normalize_embeddings=True).tolist()
        db.save_skill(skill, embedding=embedding)
        skills_created += 1

    # Verify existing skills against today's outcomes
    active_skills = db.get_active_skills()
    skills_retired = _verify_skills(active_skills, experiences)

    # Save reflection summary
    summary = {
        "date": req.date,
        "total_experiences": len(experiences),
        "winners": len(winners),
        "losers": len(losers),
        "pending_outcome": len(no_outcome),
        "qwen_analysis": qwen_response[:2000],
        "skills_created": skills_created,
        "skills_retired": skills_retired,
    }
    db.save_reflection(req.date, summary, skills_created, skills_retired, len(experiences))

    return {
        "status": "ok",
        "date": req.date,
        "experiences_analyzed": len(experiences),
        "skills_created": skills_created,
        "skills_retired": skills_retired,
        "summary": summary,
    }


# ── Reflection Helpers ──

REFLECTION_SYSTEM_PROMPT = """You are a trading strategy analyst reviewing today's paper-trading results.
Your job is to identify PATTERNS — not individual trades — and distill them into reusable RULES.

Output ONLY valid JSON: an array of skill objects. Each skill:
{
  "title": "short descriptive title",
  "rule": "actionable rule the PM agent should follow",
  "category": "entry_filter|sizing|timing|exit|risk",
  "confidence": 0.5-1.0,
  "evidence": ["list of experience_ids that support this rule"]
}

Guidelines:
- Only create a skill if you see the pattern in 2+ trades
- Be specific: "Deny 3rd entry on same symbol same day" not "Be careful with re-entries"
- Include the evidence (experience IDs) that support each rule
- Confidence should reflect how strong the pattern is (0.5 = weak, 1.0 = very strong)
- If no clear patterns, return an empty array: []
"""


def _build_reflection_prompt(
    experiences: list[Experience],
    winners: list[Experience],
    losers: list[Experience],
) -> str:
    """Build the reflection prompt from today's experiences."""
    lines = [f"## Trading Day Review: {len(experiences)} decisions\n"]
    lines.append(f"Winners: {len(winners)} | Losers: {len(losers)}\n")

    for exp in experiences:
        ctx = exp.context
        dec = exp.decision
        outcome = exp.outcome or {}
        pnl = outcome.get("pnl_pct", "pending")
        exit_r = outcome.get("exit_reason", "pending")

        lines.append(
            f"- [{exp.experience_id}] {ctx.get('symbol', '?')} "
            f"({ctx.get('signal', '?')}) → {dec.get('action', '?')} "
            f"→ {pnl}% ({exit_r})"
        )
        if headline := ctx.get("headline"):
            lines.append(f"  Headline: {headline[:100]}")
        if reasoning := dec.get("reasoning"):
            lines.append(f"  Reasoning: {reasoning[:100]}")

    lines.append("\nIdentify patterns and generate trading skills (JSON array):")
    return "\n".join(lines)


def _parse_skills_from_reflection(qwen_response: str, experiences: list[Experience]) -> list[dict]:
    """Parse skill objects from Qwen's reflection response."""
    import json

    # Try to extract JSON from the response
    text = qwen_response.strip()

    # Handle markdown code blocks
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    try:
        skills = json.loads(text)
        if isinstance(skills, list):
            return skills
        if isinstance(skills, dict) and "skills" in skills:
            return skills["skills"]
    except json.JSONDecodeError:
        logger.warning("reflection_parse_failed", extra={"response": text[:200]})

    return []


def _verify_skills(active_skills: list[Skill], experiences: list[Experience]) -> int:
    """Check existing skills against today's outcomes. Retire poor performers."""
    retired = 0
    for skill in active_skills:
        if skill.effectiveness is not None and skill.effectiveness < 0.3:
            # Low effectiveness — retire
            if db:
                db.retire_skill(skill.skill_id, f"Low effectiveness: {skill.effectiveness:.2f}")
                retired += 1
    return retired


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100)
