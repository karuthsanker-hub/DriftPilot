-- Agent layer tables for the multi-agent trading system.
-- These tables store inter-agent messages, decision logs, session parameters,
-- and agent runtime state. Used for communication, observability, and training
-- data export.
--
-- Applied by: MessageBus.initialize() (idempotent via IF NOT EXISTS)

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
