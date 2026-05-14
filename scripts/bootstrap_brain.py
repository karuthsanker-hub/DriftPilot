"""Bootstrap Brain — seed the PM Trading Brain from existing agent_decisions.

Reads all 7,285+ decisions from agent_messages.sqlite3, converts them into
experience objects, and bulk-stores them in the brain server.

Also seeds initial skills from known defects and operational lessons.

Usage:
    .venv/bin/python scripts/bootstrap_brain.py [--brain-url http://192.168.1.166:8100]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bootstrap_brain")

DEFAULT_BRAIN_URL = "http://192.168.1.166:8100"
DEFAULT_AGENT_DB = "data/driftpilot/agent_messages.sqlite3"


# ── Known defects → seed skills ──

SEED_SKILLS = [
    {
        "title": "Deny 3rd+ entry on same symbol same day",
        "rule": "If the portfolio already has 2 completed trades on this symbol today, deny the entry. Historical data shows 3rd+ entries on the same symbol same day have an 85% loss rate (defect #5).",
        "category": "entry_filter",
        "confidence": 0.9,
        "source": "defect_5",
    },
    {
        "title": "Reduce size on high-ATR names",
        "rule": "If ATR > 4%, set size_multiplier to 0.5. High-volatility names cause stop-loss slippage — software stops evaluated every ~30s can't keep up with fast movers (defect #9, TALO lost 8.12% on a 1% stop).",
        "category": "sizing",
        "confidence": 0.85,
        "source": "defect_9",
    },
    {
        "title": "Verify headline text matches sentiment label",
        "rule": "Before approving, check that the headline text actually supports the sentiment label. Qwen sometimes classifies earnings beats as neutral, or negative headlines as positive (defect #11). Words like 'downbeat', 'miss', 'cuts guidance' should be flagged regardless of sentiment label.",
        "category": "entry_filter",
        "confidence": 0.8,
        "source": "defect_11",
    },
    {
        "title": "Check price drift from catalyst time",
        "rule": "If the current price has moved more than 3% from the price at catalyst time, deny entry. The gap has already been captured. After operator restarts, the price drift cache resets, allowing stale entries (defect #12).",
        "category": "entry_filter",
        "confidence": 0.85,
        "source": "defect_12",
    },
    {
        "title": "Deny entry when Qwen/brain is timing out",
        "rule": "When the LLM is experiencing timeouts, deny entries rather than auto-approve. The fallback_action=approve pattern caused 54 unreviewed approvals on May 13. Better to miss a trade than enter without analysis.",
        "category": "risk",
        "confidence": 0.95,
        "source": "ops_playbook",
    },
    {
        "title": "Early session (first 30 min) earnings entries work best",
        "rule": "Earnings beat entries in the first 30 minutes of session have the highest win rate. After 90 minutes, the momentum from earnings gaps typically fades. Prefer early entries over late ones.",
        "category": "timing",
        "confidence": 0.65,
        "source": "ops_playbook",
    },
    {
        "title": "Consumer discretionary earnings: check beat magnitude",
        "rule": "For consumer discretionary sector earnings beats, the EPS surprise magnitude matters. Small beats (<10% above estimate) tend to stall (TIME_STOP exits). Prefer entries where the beat is >15% above estimate.",
        "category": "entry_filter",
        "confidence": 0.6,
        "source": "pattern_hypothesis",
    },
]


def load_agent_decisions(db_path: str) -> list[dict]:
    """Load all agent decisions from SQLite."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, agent_name, decision_type, symbol, slot_id,
               algo_recommendation, agent_decision, is_override,
               reasoning, confidence, llm_model, llm_latency_ms,
               prompt_version, inputs_json, raw_response,
               outcome_pnl_pct, outcome_correct, created_at
        FROM agent_decisions
        ORDER BY created_at
    """).fetchall()
    conn.close()
    logger.info(f"Loaded {len(rows)} agent decisions from {db_path}")
    return [dict(r) for r in rows]


def decision_to_experience(dec: dict) -> dict:
    """Convert an agent_decision row into a brain experience payload."""
    # Parse inputs_json for rich context
    inputs = {}
    if dec.get("inputs_json"):
        try:
            inputs = json.loads(dec["inputs_json"])
        except json.JSONDecodeError:
            pass

    context = {
        "symbol": dec.get("symbol", ""),
        "signal": inputs.get("signal_name", dec.get("decision_type", "")),
        "category": inputs.get("category", ""),
        "headline": inputs.get("headline", ""),
        "sentiment": inputs.get("sentiment", ""),
        "confidence": dec.get("confidence") or inputs.get("confidence"),
        "algo_score": inputs.get("algo_score"),
        "daily_pnl_pct": inputs.get("daily_pnl_pct"),
        "open_slots": inputs.get("open_slots"),
        "sector": inputs.get("sector", ""),
        "minutes_in_session": inputs.get("minutes_in_session"),
        "consecutive_losses": inputs.get("consecutive_losses"),
        "consecutive_wins": inputs.get("consecutive_wins"),
        "rvol": inputs.get("rvol"),
        "atr_pct": inputs.get("atr_pct"),
    }
    # Remove None values to keep embeddings clean
    context = {k: v for k, v in context.items() if v is not None}

    decision = {
        "action": dec.get("agent_decision", "unknown"),
        "reasoning": dec.get("reasoning", ""),
        "was_override": bool(dec.get("is_override")),
        "algo_recommendation": dec.get("algo_recommendation", ""),
    }

    outcome = None
    if dec.get("outcome_pnl_pct") is not None:
        outcome = {
            "pnl_pct": dec["outcome_pnl_pct"],
            "was_correct": bool(dec.get("outcome_correct")),
        }

    exp_type = "entry_decision"
    if "exit" in (dec.get("decision_type") or "").lower():
        exp_type = "exit_decision"
    elif dec.get("agent_decision") == "deny":
        exp_type = "denial"

    return {
        "experience_id": f"hist-{dec['id']}",
        "timestamp": dec.get("created_at", ""),
        "exp_type": exp_type,
        "context": context,
        "decision": decision,
        "outcome": outcome,
        "metadata": {
            "source": "bootstrap",
            "agent_name": dec.get("agent_name", ""),
            "llm_model": dec.get("llm_model", ""),
            "llm_latency_ms": dec.get("llm_latency_ms"),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Bootstrap brain from agent_decisions")
    parser.add_argument("--brain-url", default=DEFAULT_BRAIN_URL)
    parser.add_argument("--agent-db", default=DEFAULT_AGENT_DB)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--skills-only", action="store_true", help="Only seed skills, skip experiences")
    args = parser.parse_args()

    # Verify brain server is reachable
    client = httpx.Client(base_url=args.brain_url, timeout=10.0)
    try:
        resp = client.get("/brain/health")
        resp.raise_for_status()
        logger.info(f"Brain server OK: {resp.json()}")
    except Exception as e:
        logger.error(f"Brain server unreachable at {args.brain_url}: {e}")
        sys.exit(1)

    # Seed skills first
    logger.info(f"Seeding {len(SEED_SKILLS)} skills from known defects/patterns...")
    for skill_data in SEED_SKILLS:
        try:
            resp = client.post("/brain/skills/create", json=skill_data)
            resp.raise_for_status()
            result = resp.json()
            logger.info(f"  Skill created: {result['skill_id']} — {skill_data['title']}")
        except Exception as e:
            logger.warning(f"  Skill creation failed: {e}")

    if args.skills_only:
        logger.info("Skills-only mode. Done.")
        return

    # Load and convert decisions
    decisions = load_agent_decisions(args.agent_db)
    experiences = [decision_to_experience(d) for d in decisions]
    logger.info(f"Converted {len(experiences)} decisions to experiences")

    # Bulk store in batches
    total_stored = 0
    total_errors = 0
    for i in range(0, len(experiences), args.batch_size):
        batch = experiences[i : i + args.batch_size]
        try:
            resp = client.post(
                "/brain/store/bulk",
                json={"experiences": batch},
                timeout=120.0,
            )
            resp.raise_for_status()
            result = resp.json()
            total_stored += result.get("stored", 0)
            total_errors += result.get("errors", 0)
            logger.info(
                f"  Batch {i // args.batch_size + 1}: "
                f"stored={result.get('stored', 0)}, errors={result.get('errors', 0)}"
            )
        except Exception as e:
            total_errors += len(batch)
            logger.error(f"  Batch {i // args.batch_size + 1} failed: {e}")

    logger.info(f"Bootstrap complete: stored={total_stored}, errors={total_errors}")

    # Check final stats
    try:
        resp = client.get("/brain/stats")
        resp.raise_for_status()
        stats = resp.json()
        logger.info(f"Brain stats: {json.dumps(stats, indent=2)}")
    except Exception:
        pass

    client.close()


if __name__ == "__main__":
    main()
