# PM Trading Brain — Self-Evolving Portfolio Manager

> Inspired by NVIDIA Hermes agent pattern: "writes and refines its own
> skills — every time the agent encounters feedback, it saves its
> learnings as a skill."

Updated: 2026-05-14

---

## 1. Why Vector DB + RAG, Not Static Lessons

The original plan injected 10 hardcoded lessons into every prompt. Problems:
- **Same lessons for every decision** — a lesson about volatile-name sizing
  fires even when evaluating a low-vol earnings beat
- **No semantic relevance** — can't retrieve "what happened last time we
  traded an earnings beat on a consumer goods stock at 10:15 AM"
- **Doesn't scale** — capped at 10 lessons to fit context window
- **No similarity search** — can't find "situations like this one"

**RAG approach**: embed every trading experience (decision + context + outcome)
into a vector DB. At decision time, build an embedding of the current situation
and retrieve the 3-5 most similar past experiences. The PM sees *relevant*
history, not generic rules.

---

## 2. Architecture

```
┌─── Mac (Operator) ──────────────────────────────────────────────────┐
│                                                                      │
│  DriftPilot Operator                                                │
│    ├── State Machine (30s cycle)                                    │
│    ├── Catalyst Scanner                                             │
│    ├── PM Agent ── builds decision context ──┐                      │
│    ├── Slot Agents                            │                      │
│    └── PM Analyst (15-min observer)           │                      │
│                                               ▼                      │
│         ┌─────────────────────────────────────────┐                 │
│         │  Brain Client (httpx)                    │                 │
│         │  POST /brain/query   → retrieve similar  │                 │
│         │  POST /brain/store   → store experience  │                 │
│         │  POST /brain/reflect → EOD learning      │                 │
│         │  GET  /brain/skills  → active skills     │                 │
│         └──────────────┬──────────────────────────┘                 │
│                        │ HTTP                                        │
└────────────────────────┼────────────────────────────────────────────┘
                         │
┌─── DGX Spark ──────────┼────────────────────────────────────────────┐
│                        ▼                                             │
│  ┌─────────────────────────────────────────────────────────┐        │
│  │  Brain Server (FastAPI on :8100)                         │        │
│  │                                                          │        │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐ │        │
│  │  │ Embedding    │  │ ChromaDB     │  │ Skill Store   │ │        │
│  │  │ Model        │  │ (vector DB)  │  │ (SQLite)      │ │        │
│  │  │              │  │              │  │               │ │        │
│  │  │ all-MiniLM   │  │ experiences  │  │ learned rules │ │        │
│  │  │ or nomic-    │  │ + outcomes   │  │ + evidence    │ │        │
│  │  │ embed-text   │  │ + embeddings │  │ + scores      │ │        │
│  │  └──────────────┘  └──────────────┘  └───────────────┘ │        │
│  │                                                          │        │
│  │  ┌──────────────────────────────────────────────────┐   │        │
│  │  │ Reflection Engine                                 │   │        │
│  │  │ • Pattern detection across experiences            │   │        │
│  │  │ • Qwen-powered skill generation                   │   │        │
│  │  │ • Skill verification & retirement                 │   │        │
│  │  └──────────────────────────────────────────────────┘   │        │
│  └─────────────────────────────────────────────────────────┘        │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────┐        │
│  │  Qwen3-8B on vLLM (:8000)   ← already running          │        │
│  └─────────────────────────────────────────────────────────┘        │
│                                                                      │
│  Resources: 128GB unified memory, 3.3TB disk, PyTorch 2.11          │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. The Experience Loop

### 3.1 STORE — After Every Decision

When the PM Agent approves/denies an entry, or a Slot Agent evaluates a
position, the outcome is stored as a **trading experience**:

```json
{
  "experience_id": "exp-2026-05-14-001",
  "timestamp": "2026-05-14T10:35:00Z",
  "type": "entry_decision",

  "context": {
    "symbol": "YETI",
    "signal": "earnings_report_v1",
    "category": "earnings/report",
    "headline": "YETI Holdings Q1 Adj. EPS $0.26 Beats $0.18 Estimate",
    "sentiment": "positive",
    "confidence": 0.85,
    "algo_score": 0.72,
    "daily_pnl_pct": -0.5,
    "open_slots": 7,
    "sector": "Consumer Discretionary",
    "time_of_day": "10:35",
    "minutes_in_session": 65,
    "consecutive_losses": 2,
    "rvol": 3.2,
    "atr_pct": 2.1
  },

  "decision": {
    "action": "approve",
    "reasoning": "Strong beat, positive sentiment, sector not crowded",
    "target_pct": 1.0,
    "size_multiplier": 1.0,
    "was_override": false,
    "lessons_applied": ["skill-003", "skill-007"]
  },

  "outcome": {
    "pnl_pct": 1.2,
    "exit_reason": "PROFIT_TAKE",
    "hold_minutes": 8,
    "was_correct": true,
    "max_adverse_pct": -0.3,
    "max_favorable_pct": 1.4
  }
}
```

The `context` fields are embedded into a vector. The `outcome` is backfilled
when the position closes (or immediately for denials where we track the
counterfactual).

### 3.2 QUERY — Before Every Decision

When the PM Agent needs to decide on a new entry:

1. Build the **query context** from current situation (symbol, signal,
   headline, sentiment, portfolio state, time of day, etc.)
2. Embed it using the same model
3. Retrieve **top 5 most similar past experiences** from ChromaDB
4. Format them as context for the Qwen prompt:

```
RELEVANT PAST EXPERIENCES (similar situations):

[1] VSNT earnings beat ($1.99 vs $1.63), 2 days ago, approved
    → Result: +1.1% in 12 min (PROFIT_TAKE). Good entry.
    Lesson: Strong EPS beats on mid-cap consumer names work well.

[2] REZI "QuickLogic Posts Downbeat Q1", 1 day ago, approved
    → Result: -2.3% in 28 min (STOP_LOSS). Bad entry.
    Lesson: Headline said "downbeat" but was classified positive.
    Always read the actual headline words.

[3] ORCL earnings beat, 2 days ago, approved (3rd entry same day)
    → Result: -1.0% in 5 min (STOP_LOSS). Machine-gun re-entry.
    Lesson: 3rd+ entry on same symbol same day has 85% loss rate.

[4] WWW raised guidance, today, approved
    → Result: +0.8% in 15 min (PROFIT_TAKE). Good entry.

[5] GO earnings beat, similar sector, last week, approved
    → Result: -0.5% in 45 min (TIME_STOP). Stalled.
    Lesson: Grocery/consumer staples beats often stall after gap.
```

This is far more powerful than static rules — the PM sees **specific
analogous situations** with real outcomes. The LLM can pattern-match
across them naturally.

### 3.3 REFLECT — End of Day (Hermes "Skill Writing")

After market close, the Reflection Engine:

1. **Clusters** the day's experiences by outcome pattern
2. **Identifies** recurring failure/success patterns via Qwen
3. **Generates skills** — concise, reusable rules distilled from patterns:

```json
{
  "skill_id": "skill-012",
  "title": "Earnings beats on consumer discretionary: check actual magnitude",
  "rule": "For consumer discretionary earnings beats, only approve if EPS surprise is >15%. Smaller beats on these names tend to stall (4/6 recent cases were TIME_STOP).",
  "evidence": ["exp-2026-05-14-003", "exp-2026-05-13-012", ...],
  "category": "entry_filter",
  "confidence": 0.75,
  "status": "active",
  "effectiveness": null,
  "created_at": "2026-05-14T16:30:00Z"
}
```

4. **Verifies** existing skills against today's outcomes — retire if
   effectiveness drops below threshold

Skills are ALSO stored as embeddings in a separate ChromaDB collection,
retrieved alongside experiences when relevant.

### 3.4 EVOLVE — Multi-Day Pattern Building

Over days/weeks, the brain accumulates:
- **Experiences**: raw decision+outcome pairs (thousands)
- **Skills**: distilled rules from experience clusters (tens)
- **Meta-skills**: patterns across skills ("our entries work best in the
  first 90 minutes of session" — derived from noticing 5 separate skills
  all pointing to the same timing pattern)

The Reflection Engine periodically (weekly) runs a meta-analysis:
- Which skills have highest effectiveness_score?
- Are there clusters of related skills that should merge?
- Are there contradicting skills (one says "enter", evidence says "avoid")?
- Which skill categories are missing? (blind spots)

---

## 4. Technology Stack on DGX Spark

### 4.1 Embedding Model

**Option A: `nomic-ai/nomic-embed-text-v1.5`** (137M params)
- Runs on GPU alongside Qwen (minimal memory — ~300MB)
- 768-dim embeddings, 8192 token context
- Good at financial/trading text similarity

**Option B: `sentence-transformers/all-MiniLM-L6-v2`** (22M params)
- Extremely lightweight (~90MB)
- 384-dim embeddings
- Fast, good enough for structured trading contexts

**Recommendation**: Start with all-MiniLM-L6-v2 for speed. The trading
context is highly structured (same fields every time), so sophisticated
embeddings aren't critical. Upgrade to nomic if similarity quality is poor.

### 4.2 Vector Database

**ChromaDB** (pure Python, embedded, no server needed)
- Perfect for single-machine deployment on DGX
- SQLite + HNSW under the hood
- Python-native, easy to integrate
- Handles 100K+ vectors easily with <1GB RAM
- Persistence to disk built-in

**Why not FAISS**: No built-in metadata filtering. We need to filter by
date, symbol, signal type, etc. alongside vector similarity.

**Why not Milvus/Qdrant**: Overkill for single-machine. ChromaDB's
simplicity wins for our scale (tens of thousands of experiences, not millions).

### 4.3 Brain Server (FastAPI on DGX, port 8100)

Runs alongside vLLM on the DGX. The server owns:
- ChromaDB instance (persistent, disk-backed)
- Embedding model (loaded once on startup)
- Skill store (SQLite)
- Reflection engine (calls Qwen via localhost:8000)

Memory budget on DGX:
- Qwen3-8B via vLLM: ~18GB (at 90% GPU util)
- Embedding model: ~0.3GB
- ChromaDB: ~1GB (for 100K experiences)
- Brain server: ~0.5GB
- **Total**: ~20GB of 128GB = plenty of headroom

---

## 5. Brain Server API

```
POST /brain/store
  Body: { experience object }
  → Embed context, store in ChromaDB + metadata
  → Return experience_id

POST /brain/query
  Body: { context object, top_k: 5, filters: { signal?, date_range? } }
  → Embed context, search ChromaDB
  → Return top_k similar experiences with outcomes
  → Also return relevant active skills

POST /brain/backfill
  Body: { experience_id, outcome object }
  → Update experience with outcome data
  → Re-embed if outcome changes the experience text

POST /brain/reflect
  Body: { date: "2026-05-14" }
  → Cluster day's experiences
  → Call Qwen for pattern analysis
  → Generate/update skills
  → Verify existing skills
  → Return reflection summary

GET /brain/skills
  Query: ?status=active&applies_to=pm_entry_approval
  → Return active skills for prompt injection

GET /brain/stats
  → Return brain health: total experiences, skills, effectiveness

POST /brain/skill/{skill_id}/retire
  → Manually retire a skill

GET /brain/similar/{experience_id}
  → Find experiences similar to a given one (debugging)
```

---

## 6. Integration with Existing Agent Layer

### 6.1 PM Agent Entry Approval (modified flow)

```python
# In pm_agent.py, before calling LLM:

# 1. Build decision context
context = {
    "symbol": candidate.symbol,
    "signal": candidate.signal_name,
    "headline": candidate.headline,
    "sentiment": candidate.sentiment,
    # ... (all fields from current pm_entry_approval.yaml user_template)
}

# 2. Query brain for similar experiences + active skills
brain_response = await brain_client.query(context, top_k=5)

# 3. Format for prompt injection
experience_block = format_experiences(brain_response.experiences)
skill_block = format_skills(brain_response.skills)

# 4. Augment the prompt
augmented_user_content = f"""
{original_user_template.format(**template_vars)}

RELEVANT PAST EXPERIENCES:
{experience_block}

ACTIVE TRADING RULES (learned from experience):
{skill_block}
"""

# 5. Call Qwen with augmented prompt
decision = await llm_client.complete(system_prompt, augmented_user_content)

# 6. Store the decision as a new experience (outcome TBD)
await brain_client.store({
    "context": context,
    "decision": decision,
    "skills_applied": [s.skill_id for s in brain_response.skills]
})
```

### 6.2 Outcome Backfill (on position close)

```python
# In the exit flow, after position closes:
await brain_client.backfill(experience_id, {
    "pnl_pct": realized_pnl / slot_value,
    "exit_reason": exit_reason,
    "hold_minutes": hold_minutes,
    "was_correct": pnl > 0,
    "max_adverse_pct": max_drawdown,
    "max_favorable_pct": max_runup,
})
```

### 6.3 EOD Reflection (triggered by daily_stop.sh)

```python
# After market close, before shutdown:
reflection = await brain_client.reflect(date=today)
# Results stored in brain, visible on dashboard
```

---

## 7. Counterfactual Tracking

Critical for learning: when PM **denies** an entry, we still track what
WOULD have happened:

```python
# After PM denies:
# 1. Store the denial as an experience
# 2. Track the symbol's price for the next 60 minutes
# 3. Backfill the counterfactual outcome:
#    - If it would have been profitable → denial was WRONG (update brain)
#    - If it would have lost money → denial was CORRECT (reinforce)
```

This gives the brain signal on BOTH approvals and denials. Without
counterfactuals, the brain only learns from trades taken, never from
trades avoided.

Implementation: a background task queries Alpaca for price data on
denied symbols at entry_time + 60 minutes. Runs hourly during market
hours.

---

## 8. Dashboard Additions

New `/brain` page on the dashboard:

### Brain Overview Panel
- Total experiences stored (with growth trend)
- Active skills count and average effectiveness
- Today's brain queries and hit rate
- Memory utilization on DGX

### Experience Explorer
- Searchable list of past experiences
- Filter by symbol, signal, outcome, date
- Click to see similar experiences (vector search demo)

### Skill Manager
- List all skills with status, confidence, effectiveness
- Activate/retire/suspend skills manually
- Click skill → see evidence (linked experiences)
- Effectiveness trend chart over time

### Learning Timeline
- Daily reflection summaries
- Skills created/retired per day
- Brain performance trend (did decisions improve over time?)

---

## 9. Implementation Phases

### Phase 1: Brain Server on DGX (2-3 days)

**Install on DGX:**
```bash
pip install chromadb sentence-transformers fastapi uvicorn
```

**Build:**
- `dgx/brain_server.py` — FastAPI server with /store, /query, /skills
- `dgx/brain_embedder.py` — Embedding model wrapper
- `dgx/brain_db.py` — ChromaDB + SQLite skill store
- Test with mock data

**Deliverable**: Brain server running on DGX:8100, responding to
store/query/skills requests.

### Phase 2: Operator Integration (2 days)

**Build:**
- `src/driftpilot/agents/brain_client.py` — httpx client for brain API
- Modify `pm_agent.py` to query brain before decisions
- Modify exit flow to backfill outcomes
- Add brain health check to operator startup

**Deliverable**: PM Agent queries the brain for every entry decision.
Experiences stored and backfilled.

### Phase 3: Reflection Engine (2-3 days)

**Build:**
- `dgx/brain_reflection.py` — Pattern detection + Qwen skill generation
- `config/prompts/brain_eod_review.yaml` — EOD reflection prompt
- Wire into daily_stop.sh
- Counterfactual tracking for denied entries

**Deliverable**: End-of-day reflection generates skills. Skills injected
into future prompts.

### Phase 4: Verification + Evolution (1-2 days)

**Build:**
- Skill effectiveness scoring
- Auto-retire bad skills
- Confidence decay for stale skills
- Meta-analysis (weekly skill clustering)

**Deliverable**: Fully autonomous learning loop.

### Phase 5: Dashboard + Bootstrap (2 days)

**Build:**
- Brain dashboard page
- Seed brain with 7,285 existing decisions
- Historical reflection on past trading days

**Deliverable**: Full visibility, bootstrapped brain.

---

## 10. Bootstrap: Seeding the Brain

### From Existing Data (Day 1)

1. Export all 7,285 `agent_decisions` as experiences
2. Backfill outcomes from `positions` table
3. Embed and store in ChromaDB
4. Run reflection on each past trading day to generate initial skills

### From Defects (Immediate)

Convert known defects into skills:

| Defect | Skill |
|--------|-------|
| #5 Machine-gun re-entry | "Deny 3rd+ entry on same symbol same day" |
| #9 Stop slippage | "Reduce size_multiplier to 0.5 for ATR > 4%" |
| #11 Sentiment misclass | "Verify headline text matches sentiment label" |
| #12 Drift cache reset | "Check if symbol price moved >3% from catalyst time" |
| Qwen timeouts | "When Qwen times out, deny entry (don't auto-approve)" |

### From Daily Ops Playbook (Immediate)

Convert operational lessons into skills with source="ops_playbook".

---

## 11. Safety Invariants

1. **Guardrails untouched** — Brain influences LLM reasoning only.
   GuardrailValidator runs BEFORE and AFTER brain augmentation.
2. **Override rate < 20%** — Checked mechanically, brain cannot bypass.
3. **Graceful degradation** — If brain server unreachable, PM Agent falls
   back to static prompts (current behavior). Brain is additive only.
4. **No hallucinated experiences** — Only real decisions+outcomes stored.
   Qwen generates skills from real data, never fabricates experiences.
5. **Audit trail** — Every query logged with which experiences/skills
   were retrieved and how they influenced the decision.
6. **Kill switch** — `BRAIN_ENABLED=false` in .env disables all brain
   queries. System reverts to pre-brain behavior instantly.

---

## 12. Success Metrics (4-week paper trading)

| Metric | Without Brain | Target With Brain |
|--------|--------------|-------------------|
| PM decision accuracy | ~55% | ≥ 65% |
| Repeat-mistake rate | ~30% | < 10% |
| Avg winner (from dynamic targets) | 1.0% | ≥ 1.3% |
| Avg loser (from early cuts) | -1.0% | ≤ -0.8% |
| Skill count (active) | 0 | 15-30 |
| Skill effectiveness (avg) | N/A | ≥ 0.6 |
| Brain query latency p95 | N/A | < 200ms |
| Experience DB size | 0 | 5,000+ |
